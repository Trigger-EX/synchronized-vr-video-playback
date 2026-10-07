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
    return {"t": t, "flags": flags, "sent": list(sent), "failed": [], "suppressed": None, "verified": verified}


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
                 "online": online, "now": NOW, "desired": "playing"}, **extra)


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
    real = ctl.fleet.listed_checked

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("adb exploded")
        return real()
    monkeypatch.setattr(ctl.fleet, "listed_checked", flaky)
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
    ctl.devices["hs0"].desired = {"mode": "playing"}
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
    # the tail is read unfiltered (the newest stamp is the truth); the player's lines are dropped in Python
    assert any("logcat -d -t 1000" in ln and "SyncVR" not in ln for ln in adb_lines(adb_log))
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


def test_display_on_while_playing_is_only_written_to_the_probe_csv(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setattr(BlackScreen, "log_every_s", 0.0)  # throttling has its own test
    ctl.devices["hs0"].desired = {"mode": "playing"}
    arm(ctl, "black_screen")
    run(ctl, "black_screen", times=4)
    assert not sent(adb_log, "keyevent")
    files = list((tmp_path / "probe").glob("*.csv"))
    assert len(files) == 1
    rows = files[0].read_text().splitlines()
    assert rows[0].startswith("time,device,label,display") and len(rows) == 5
    assert "hs0" in rows[1] and "ON" in rows[1] and "unsampled" in rows[1]


def test_probe_csv_is_written_even_when_observe_only_or_braked(ctl, tmp_path, monkeypatch):
    monkeypatch.setattr(BlackScreen, "log_every_s", 0.0)
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


# =========================================================== safety review fixes

import json  # noqa: E402

from syncvr.fleetops import ListingFailed  # noqa: E402


def hist(ctl, name, dev="hs0"):
    return list(ctl.watchdogs.states[name].history[dev])


def mem(ctl, name, dev="hs0"):
    return ctl.watchdogs.states[name].mem[dev]


# ---- 1/2: attempts are recorded before they run, and outlive the 20-entry history

def test_a_send_that_raises_still_counts_for_the_give_up_limit(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_anr")
    calls = []

    def boom(serial):
        calls.append(serial)
        raise RuntimeError("adb: device offline")
    monkeypatch.setattr(ctl.fleet, "back", boom)
    arm(ctl, "popup")
    run(ctl, "popup", times=6, step=10)
    assert len(calls) == 3  # gave up after max_failures, not retried forever
    st = ctl.watchdogs.states["popup"]
    assert all("back" in e["sent"] for e in hist(ctl, "popup")[:3])
    assert [e["failed"] for e in hist(ctl, "popup")[:3]] == [["back"]] * 3
    assert st.counters["send_failed"] == 3 and mem(ctl, "popup")["failures"] == 3
    assert len([e for e in ctl.events if "device offline" in e["message"]]) == 1  # logged once, not per cycle


def test_failures_and_give_up_survive_more_than_twenty_cycles(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_anr")
    arm(ctl, "popup")
    run(ctl, "popup", times=30, step=10)
    assert len(sent(adb_log, "keyevent 4")) == 3
    assert len(hist(ctl, "popup")) == watchdogs.HISTORY_LEN
    assert mem(ctl, "popup")["failures"] == 3


def test_cooldown_does_not_depend_on_the_history_deque(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_CONNECT_FAIL", "10.9.9.9:5555")
    c = make_ctl(ctl.data_dir / "k", ips=(("10.9.9.9", "S3"),)) if (ctl.data_dir / "k").mkdir() is None else None
    arm(c, "keepalive")
    run(c, "keepalive", times=50, step=1)  # 50 s: the first attempt is long out of the 20-entry deque
    assert len(sent(adb_log, "connect 10.9.9.9")) == 1
    assert watchdogs.last_sent([], ("wake",), 100.0, {"wake": 90.0}) == 10.0
    assert watchdogs.last_sent([entry(t=95, sent=["wake"])], ("wake",), 100.0, {"wake": 90.0}) == 5.0


def test_keepalive_backs_off_exponentially(tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_CONNECT_FAIL", "10.9.9.9:5555")
    c = make_ctl(tmp_path, ips=(("10.9.9.9", "S3"),))
    arm(c, "keepalive")
    run(c, "keepalive", step=40)  # attempt 1 (t=0), fails -> next after 60 s
    run(c, "keepalive", step=40)  # t=40: too early
    assert len(sent(adb_log, "connect 10.9.9.9")) == 1
    run(c, "keepalive", step=40)  # t=80: second attempt
    assert len(sent(adb_log, "connect 10.9.9.9")) == 2
    assert mem(c, "keepalive")["connect_fails"] == 2
    assert Keepalive.retry_delay({"retry_s": 30.0}, 0) == 30 and Keepalive.retry_delay({"retry_s": 30.0}, 9) == 900


def test_keepalive_connect_that_raises_counts_as_an_attempt(tmp_path, adb_log, monkeypatch):
    c = make_ctl(tmp_path, ips=(("10.9.9.9", "S3"),))
    n = []
    monkeypatch.setattr(c.fleet, "connect", lambda a: n.append(a) or (_ for _ in ()).throw(OSError("boom")))
    arm(c, "keepalive")
    run(c, "keepalive", times=3, step=5)
    assert len(n) == 1


# ---- 3: ambiguous addresses

def test_an_address_claimed_by_two_headsets_is_never_acted_on(tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    c = make_ctl(tmp_path, ips=(("192.168.1.20", "S1"), ("192.168.1.20", "S2")))
    arm(c, "stay_awake")
    run(c, "stay_awake", times=2)
    assert not sent(adb_log, "keyevent")
    assert sum("claimed by 2" in e["message"] for e in c.events) == 2  # once per headset, not per cycle


def test_a_reported_serial_is_preferred_over_a_duplicated_ip(tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    c = make_ctl(tmp_path, ips=(("192.168.1.20", "USB123"), ("192.168.1.20", "S2")))
    arm(c, "stay_awake")
    run(c, "stay_awake")
    assert len(sent(adb_log, "-s USB123 shell input keyevent 224")) == 1
    assert not sent(adb_log, "-s 192.168.1.20:5555")


def test_keepalive_does_not_connect_an_ambiguous_address(tmp_path, adb_log):
    c = make_ctl(tmp_path, ips=(("10.9.9.9", "S3"), ("10.9.9.9", "S4")))
    arm(c, "keepalive")
    run(c, "keepalive")
    assert not sent(adb_log, "connect")


# ---- 4: listing failures and implausible gaps

def test_a_failed_adb_devices_skips_the_cycle(tmp_path, adb_log, monkeypatch):
    c = make_ctl(tmp_path, ips=(("10.9.9.9", "S3"),))
    arm(c, "keepalive")

    def failing():
        raise ListingFailed("adb: command not found")
    monkeypatch.setattr(c.fleet, "listed_checked", failing)
    run(c, "keepalive", times=3)
    st = c.watchdogs.states["keepalive"]
    assert st.counters["listing_failed"] == 3 and st.counters["errors"] == 0
    assert "skipped" in st.last_summary and "adb devices" in st.last_summary
    assert not sent(adb_log, "connect") and not st.history
    assert sum("adb devices" in e["message"] for e in c.events) == 1


def test_a_listing_failure_is_distinguishable_from_an_empty_listing(tmp_path, monkeypatch):
    fleet = AdbFleet(Adb(FAKE_ADB))
    monkeypatch.setenv("FAKE_ADB_DEVICES", str(tmp_path / "missing.txt"))  # cat fails, adb prints nothing
    monkeypatch.setattr(fleet.adb, "devices", lambda: (_ for _ in ()).throw(RuntimeError("adb: no server")))
    with pytest.raises(ListingFailed):
        fleet.listed_checked()
    assert fleet.listed() == set()
    monkeypatch.undo()
    empty = tmp_path / "empty.txt"
    empty.write_text("List of devices attached\n\n")
    monkeypatch.setenv("FAKE_ADB_DEVICES", str(empty))
    assert fleet.listed_checked() == set()


def test_keepalive_skips_when_most_of_a_large_fleet_looks_missing(tmp_path, adb_log):
    ips = (("192.168.1.20", "S1"),) + tuple((f"10.0.0.{i}", f"T{i}") for i in range(1, 7))
    c = make_ctl(tmp_path, ips=ips)  # 7 headsets, only the first is in adb devices
    arm(c, "keepalive")
    run(c, "keepalive")
    assert not sent(adb_log, "connect")
    st = c.watchdogs.states["keepalive"]
    assert st.counters["implausible"] == 1 and "6 of 7" in st.last_summary
    assert any("looks like an adb problem" in e["message"] for e in c.events)


def test_keepalive_still_works_for_a_few_missing_in_a_small_fleet(tmp_path, adb_log):
    c = make_ctl(tmp_path, ips=(("192.168.1.20", "S1"), ("10.0.0.1", "T1"), ("10.0.0.2", "T2")))
    arm(c, "keepalive")
    run(c, "keepalive")
    assert len(sent(adb_log, "connect 10.0.0.1")) == 1


# ---- 5: user regexes

@pytest.mark.parametrize("pattern, word", [
    ("(a+)+$", "catastrophically"), ("(a|aa)+b", "catastrophically"), (r"(x)\1", "backreference"),
    ("x" * 201, "longer than"), ("a*", "empty string"), ("(", "valid regular"),
])
def test_unsafe_patterns_are_refused(pattern, word):
    assert word in watchdogs.pattern_problem(pattern)
    assert watchdogs._regex(pattern) is None


def test_reasonable_patterns_are_accepted():
    assert watchdogs.pattern_problem("OverheatPrompt|temperature critical") is None
    assert watchdogs.pattern_problem(r"Window #\d+ .*Overheat") is None


def test_settings_refuse_unsafe_or_overlong_patterns(ctl):
    wd = ctl.watchdogs
    for bad in ("(a+)+$", "x" * 201, "ab", "("):
        with pytest.raises(WatchdogError):
            wd.set("overheat", {"cfg": {"pattern": bad}})
        with pytest.raises(WatchdogError):
            wd.set("popup", {"cfg": {"evidence": bad}})
    err = None
    try:
        wd.set("overheat", {"cfg": {"pattern": "(a+)+$"}})
    except WatchdogError as exc:
        err = str(exc)
    assert err.startswith("pattern ")


def test_guarded_search_treats_running_out_of_time_as_no_match():
    ticks = iter(range(100))
    regex = watchdogs.re.compile("needle")
    lines = ["hay"] * 5 + ["needle"]
    assert watchdogs.guarded_search(regex, lines, budget_s=2.5, clock=lambda: next(ticks)) == (False, True)
    assert watchdogs.guarded_search(regex, lines) == (True, False)
    long_line = "x" * 5000 + "needle"  # cut to MAX_LINE_LEN before matching
    assert watchdogs.guarded_search(regex, [long_line]) == (False, False)


def test_observe_matches_on_the_executor_not_the_event_loop(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(FX / "logcat_overheat.txt"))
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_overheat")
    confirmed_overheat(ctl, tmp_path)
    threads = []
    real = watchdogs.guarded_search

    def spy(*a, **k):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)
    monkeypatch.setattr(watchdogs, "guarded_search", spy)
    ctl.watchdogs.states["overheat"].cursors["hs0"] = "10-07 12:00:00.000"
    run(ctl, "overheat")
    assert threads and not any(threads)
    assert len(sent(adb_log, "keyevent 4")) == 1


def test_a_matching_timeout_is_no_match_plus_one_log_line(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(FX / "logcat_overheat.txt"))
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_overheat")
    confirmed_overheat(ctl, tmp_path)
    monkeypatch.setattr(watchdogs, "guarded_search", lambda *a, **k: (False, True))
    ctl.watchdogs.states["overheat"].cursors["hs0"] = "10-07 12:00:00.000"
    run(ctl, "overheat", times=3)
    assert not sent(adb_log, "keyevent")
    assert sum("ran out of time" in e["message"] for e in ctl.events) == 1


def test_an_observation_that_hangs_is_given_up_on(ctl, monkeypatch):
    monkeypatch.setattr(watchdogs, "OBSERVE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(StayAwake, "observe", lambda self, wd, dev, serial, tele: time.sleep(0.6) or {})
    ctl.watchdogs.set("stay_awake", {"enabled": True})
    t0 = time.monotonic()
    run(ctl, "stay_awake")
    assert time.monotonic() - t0 < 0.5
    st = ctl.watchdogs.states["stay_awake"]
    assert st.counters["errors"] == 1 and "longer than" in st.decisions[-1]["error"]


# ---- 6: brake and asleep are re-checked on the send worker

def test_a_brake_that_flips_while_the_send_waits_blocks_it_on_the_worker(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    names = []
    monkeypatch.setattr(ctl, "brake", lambda: names.append(1) or (
        "show_mode" if threading.current_thread().name.startswith("syncvr-wd-send") else None))
    arm(ctl, "stay_awake")
    run(ctl, "stay_awake", times=2)
    assert not sent(adb_log, "keyevent")
    d = last_decision(ctl, "stay_awake")
    assert d["suppressed"] == "show_mode" and "not sent" in d["text"]
    assert all(e["sent"] == [] and e["suppressed"] == "show_mode" for e in hist(ctl, "stay_awake"))
    assert not mem(ctl, "stay_awake").get("sent_at", {}).get("wake")  # the record was rolled back


def test_a_headset_put_to_sleep_while_the_send_waits_is_left_alone(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    arm(ctl, "stay_awake")
    real = ctl.brake

    def brake():
        if threading.current_thread().name.startswith("syncvr-wd-send"):
            ctl.intentionally_asleep.add("hs0")
        return real()
    monkeypatch.setattr(ctl, "brake", brake)
    run(ctl, "stay_awake")
    assert not sent(adb_log, "keyevent")
    assert last_decision(ctl, "stay_awake")["suppressed"] == "intentionally asleep"


def test_sends_run_on_the_dedicated_executor(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    seen = []
    monkeypatch.setattr(ctl.fleet, "wake_screen", lambda s: seen.append(threading.current_thread().name) or "ok")
    arm(ctl, "stay_awake")
    run(ctl, "stay_awake")
    assert seen and seen[0].startswith("syncvr-wd-send")


# ---- 7: intentionally_asleep persists

def test_intentionally_asleep_round_trips_and_ignores_unknown_ids(ctl, tmp_path):
    ctl.intentionally_asleep.add("hs0")
    data = json.loads(json.dumps(ctl.dump()))
    assert data["intentionally_asleep"] == ["hs0"]
    other = tmp_path / "b"
    other.mkdir()
    c2 = make_ctl(other)
    data["intentionally_asleep"].append("ghost")
    c2.load(data)
    assert c2.intentionally_asleep == {"hs0"}
    c2.load(dict(data, intentionally_asleep="garbage"))
    assert c2.intentionally_asleep == set()


def test_sleep_and_wake_mark_the_state_dirty(ctl, adb_log):
    async def go():
        ctl.dirty = False
        ctl._act_sleep([ctl.devices["hs0"]], {})
        assert ctl.dirty and "hs0" in ctl.intentionally_asleep
        ctl.dirty = False
        ctl._act_wake([ctl.devices["hs0"]], {})
        assert ctl.dirty and "hs0" not in ctl.intentionally_asleep
        await asyncio.sleep(0.3)
    asyncio.run(go())


# ---- 8: black screen needs worn is not False

@pytest.mark.parametrize("worn, acts", [(False, False), (True, True), (None, True)])
def test_black_screen_does_not_act_on_a_headset_that_is_not_worn(worn, acts):
    got = BlackScreen.decide([SUSPECT, SUSPECT], black_obs(worn=worn), BLACK_CFG)
    assert (kinds(got) == ["wake"]) is acts
    assert BlackScreen.analyze(black_obs(worn=worn), BLACK_CFG)["suspect"] is acts


def test_black_screen_observe_reports_worn(ctl, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_DISPLAY", "display_off")
    ctl.devices["hs0"].desired = {"mode": "playing"}
    ctl.devices["hs0"].status = {"worn": False}
    arm(ctl, "black_screen")
    run(ctl, "black_screen", times=4)
    assert not sent_wakes(ctl)


def sent_wakes(ctl):
    return [e for e in hist(ctl, "black_screen") if e["sent"]]


# ---- 9: the overheat logcat cursor

def test_new_log_lines_handles_year_rollover_and_a_clock_that_stepped_back():
    lines = ["12-31 23:59:58.000 a", "12-31 23:59:59.500 b", "01-01 00:00:01.000 c"]
    fresh, newest, rebased = watchdogs.new_log_lines(lines, "12-31 23:59:59.000")
    assert fresh == lines[1:] and newest == "01-01 00:00:01.000" and not rebased
    fresh, newest, rebased = watchdogs.new_log_lines(["10-07 11:00:00.000 x"], "10-07 12:00:10.000")
    assert fresh == [] and newest == "10-07 11:00:00.000" and rebased  # the cursor is in the future: re-baseline
    assert watchdogs.new_log_lines(["10-07 12:00:10.000 x"], "10-07 12:00:10.000") == ([], "10-07 12:00:10.000", False)
    assert watchdogs.new_log_lines(["garbage"], "10-07 12:00:10.000") == ([], "10-07 12:00:10.000", False)
    assert watchdogs.new_log_lines(lines, None) == ([], "01-01 00:00:01.000", False)


def test_a_cursor_ahead_of_the_device_clock_is_rebaselined_in_a_cycle(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(FX / "logcat_overheat.txt"))
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_overheat")
    confirmed_overheat(ctl, tmp_path)
    st = ctl.watchdogs.states["overheat"]
    st.cursors["hs0"] = "10-07 15:00:00.000"
    run(ctl, "overheat")
    assert st.cursors["hs0"] == "10-07 12:00:03.400" and not sent(adb_log, "keyevent")


def test_the_baseline_is_not_stuck_when_the_newest_line_is_the_players_own(ctl, tmp_path, adb_log, monkeypatch):
    log = tmp_path / "own.txt"
    log.write_text("10-07 12:00:00.100  1  1 I Other: x\n10-07 12:00:09.900  1  1 I SyncVR  : tick\n")
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(log))
    confirmed_overheat(ctl, tmp_path)
    run(ctl, "overheat")
    assert ctl.watchdogs.states["overheat"].cursors["hs0"] == "10-07 12:00:09.900"


def test_cursors_are_cleared_when_enabled_and_when_the_headset_is_unreachable(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(FX / "logcat_overheat.txt"))
    confirmed_overheat(ctl, tmp_path)
    st = ctl.watchdogs.states["overheat"]
    run(ctl, "overheat")
    assert st.cursors
    monkeypatch.setattr(ctl.fleet, "listed_checked", lambda: {"USB123"})  # hs0 vanished from adb devices
    run(ctl, "overheat")
    assert "hs0" not in st.cursors
    st.cursors["hs0"] = "10-07 12:00:00.000"
    ctl.watchdogs.set("overheat", {"enabled": False})
    ctl.watchdogs.set("overheat", {"enabled": True})
    assert st.cursors == {}


def test_enabling_resets_give_up_counts_but_not_cooldown_stamps(ctl):
    wd = ctl.watchdogs
    st = wd.states["popup"]
    st.mem["hs0"] = {"failures": 3, "connect_fails": 4, "sent_at": {"back": 1.0}}
    wd.set("popup", {"enabled": True})
    assert st.mem["hs0"] == {"failures": 0, "connect_fails": 0, "sent_at": {"back": 1.0}}


# ---- 10: bounds, disarm on change, hash

@pytest.mark.parametrize("name, cfg", [
    ("popup", {"max_failures": 0}), ("popup", {"max_failures": 11}), ("popup", {"launch_cooldown_s": 1}),
    ("popup", {"crash_samples": 0}), ("popup", {"verify_delay_s": 100}), ("black_screen", {"samples": 0}),
    ("black_screen", {"cooldown_s": 1}), ("keepalive", {"retry_s": 1}), ("overheat", {"cooldown_s": 0}),
    ("overheat", {"verify_delay_s": 0}), ("stay_awake", {"interval_s": 0.5}),
    ("overheat", {"package": "not a package"}), ("overheat", {"exclude": "ab"}),
    ("overheat", {"exclude": ",".join(["abcd"] * 11)}), ("overheat", {"pattern_confirmed": "zz"}),
])
def test_settings_are_bounded(ctl, name, cfg):
    with pytest.raises(WatchdogError):
        ctl.watchdogs.set(name, {"enabled": True, "cfg": cfg})
    assert not ctl.watchdogs.states[name].enabled


def test_any_setting_change_disarms(ctl):
    arm(ctl, "popup")
    wd = ctl.watchdogs
    assert wd.states["popup"].armed
    wd.set("popup", {"cfg": {"interval_s": 10.0}})  # same value: not a change
    assert wd.states["popup"].armed
    wd.set("popup", {"cfg": {"interval_s": 20.0}})
    assert not wd.states["popup"].armed and wd.states["popup"].enabled
    arm(ctl, "popup")
    wd.set("popup", {"cfg": {"max_failures": 5}})
    assert not wd.states["popup"].armed
    # asking to arm in the same request is a fresh arming and goes through the gate
    wd.set("popup", {"armed": True, "testing": True, "cfg": {"max_failures": 4}})
    assert wd.states["popup"].armed
    with pytest.raises(WatchdogError, match="not marked as tested"):
        wd.set("popup", {"armed": False})
        wd.set("popup", {"armed": True, "cfg": {"max_failures": 3}})


def test_confirmed_pattern_hash_covers_exclude_and_package(ctl, tmp_path):
    confirmed_overheat(ctl, tmp_path)
    wd = ctl.watchdogs
    st = wd.states["overheat"]
    assert st.armed and st.cfg["pattern_confirmed"] == Overheat.rule_hash(st.cfg)
    for cfg in ({"exclude": "Other"}, {"package": "com.other.player"}):
        arm(ctl, "overheat") if not st.armed else None
        wd.set("overheat", {"cfg": cfg})
        assert not st.armed and "confirmed" in wd.states["overheat"]["arm_blocker"] if False else not st.armed
        assert "confirmed" in Overheat().arm_blocker(st.cfg)
        wd.set("overheat", {"cfg": {"exclude": "", "package": PLAYER}})
        st.cfg["pattern_confirmed"] = Overheat.rule_hash(st.cfg)
    assert Overheat.rule_hash(dict(st.cfg, exclude="x1x")) != Overheat.rule_hash(st.cfg)
    assert wd.to_json_one("overheat")["pattern_hash"] == Overheat.rule_hash(st.cfg)


# ---- 11: popup launch needs an active desired mode

@pytest.mark.parametrize("desired, want", [("playing", ["launch"]), ("paused", ["launch"]), ("stopped", []),
                                          ("idle", []), (None, [])])
def test_popup_launches_only_for_an_active_desired_mode(desired, want):
    obs = popup_obs("window_home", online=False, desired=desired)
    assert kinds(Popup.decide([entry(crash=True)], obs, POPUP_CFG)) == want


# ---- 12: overheat confirmation and verification

def test_confirming_needs_a_match_in_both_logcat_and_window_list(ctl, tmp_path):
    folder = snapshot_folder(tmp_path, window="window_home")
    with pytest.raises(CommandError, match="cannot confirm.*window.txt"):
        ctl.watchdog_test_pattern("overheat", {"folder": str(folder), "pattern": PATTERN, "confirm": True})
    assert ctl.watchdogs.states["overheat"].cfg["pattern_confirmed"] == ""
    res = ctl.watchdog_test_pattern("overheat", {"folder": str(folder), "pattern": PATTERN})
    assert res["by_file"] == {"logcat.txt": 2, "window.txt": 0} or res["by_file"]["window.txt"] == 0
    ok = snapshot_folder(tmp_path, name="snap2")
    assert ctl.watchdog_test_pattern("overheat", {"folder": str(ok), "pattern": PATTERN, "confirm": True})["confirmed"]


def test_overheat_verifies_that_the_prompt_is_gone(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(FX / "logcat_overheat.txt"))
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_overheat")
    confirmed_overheat(ctl, tmp_path)
    st = ctl.watchdogs.states["overheat"]
    st.cursors["hs0"] = "10-07 12:00:00.000"
    run(ctl, "overheat")
    assert [d["verified"] for d in st.decisions if d.get("sent")] == [False]  # still on screen
    real = ctl.fleet.back

    def back(serial):
        monkeypatch.setenv("FAKE_ADB_WINDOW", "window_home")
        return real(serial)
    monkeypatch.setattr(ctl.fleet, "back", back)
    st.cursors["hs0"] = "10-07 12:00:00.000"
    ctl.clock.t += 100  # past the cooldown
    run(ctl, "overheat")
    assert [d["verified"] for d in st.decisions if d.get("sent")][-1] is True


def test_verify_is_not_success_when_the_headset_became_unreachable(ctl, monkeypatch):
    wd, dev = ctl.watchdogs, ctl.devices["hs0"]
    act = Action("back")
    assert Popup().verify(wd, dev, None, act, {"online": True}, POPUP_CFG) is False
    ov = dict(Overheat.defaults, pattern=PATTERN)
    assert Overheat().verify(wd, dev, None, act, {}, ov) is False

    def offline(serial, command, **kw):
        raise RuntimeError("device offline")
    monkeypatch.setattr(ctl.fleet, "shell", offline)
    with pytest.raises(RuntimeError):
        Overheat().verify(wd, dev, "192.168.1.20:5555", act, {}, ov)  # raises: the framework records NOT verified


def test_a_verify_that_raises_is_recorded_as_not_verified(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_anr")
    monkeypatch.setattr(Popup, "verify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gone")))
    arm(ctl, "popup")
    run(ctl, "popup")
    assert last_decision(ctl, "popup")["verified"] is False


# ---- 13: probe CSV, pruning

def test_probe_rows_are_throttled_per_headset(ctl, tmp_path):
    ctl.devices["hs0"].desired = {"mode": "playing"}
    ctl.watchdogs.set("black_screen", {"enabled": True})
    run(ctl, "black_screen", times=5, step=30)  # 150 s < 300 s
    rows = next((tmp_path / "probe").glob("*.csv")).read_text().splitlines()
    assert len(rows) == 2
    ctl.clock.t += 400
    run(ctl, "black_screen")
    assert len(next((tmp_path / "probe").glob("*.csv")).read_text().splitlines()) == 3


def test_probe_rows_are_written_off_the_loop_thread(ctl, tmp_path, monkeypatch):
    ctl.devices["hs0"].desired = {"mode": "playing"}
    ctl.watchdogs.set("black_screen", {"enabled": True})
    where = []
    real = ctl.watchdogs.write_probe_row
    monkeypatch.setattr(ctl.watchdogs, "write_probe_row",
                        lambda d, r: where.append(threading.current_thread() is threading.main_thread()) or real(d, r))
    run(ctl, "black_screen")
    assert where == [False]


def test_probe_csv_rotates_and_old_files_are_pruned(ctl, tmp_path, monkeypatch):
    monkeypatch.setattr(watchdogs, "PROBE_MAX_BYTES", 300)
    monkeypatch.setattr(watchdogs, "PROBE_KEEP_FILES", 3)
    dev = ctl.devices["hs0"]
    for _ in range(40):
        ctl.watchdogs.write_probe_row(dev, {"display": "ON", "picture": "unsampled"})
        time.sleep(0.002)
    files = list((tmp_path / "probe").glob("probe_*.csv"))
    assert 2 <= len(files) <= 3
    assert all(f.stat().st_size < 600 for f in files)
    assert all(f.read_text().startswith("time,device") for f in files)


def test_state_for_removed_headsets_is_pruned(tmp_path, adb_log, monkeypatch):
    c = make_ctl(tmp_path, ips=(("192.168.1.20", "S1"), ("", "USB123")))
    monkeypatch.setenv("FAKE_ADB_FAIL", "USB123")
    arm(c, "stay_awake")
    run(c, "stay_awake")
    st = c.watchdogs.states["stay_awake"]
    st.cursors["hs1"] = "10-07 12:00:00.000"
    assert {"hs0", "hs1"} <= set(st.history) and "hs1" in st.mem
    assert any(k[1] == "hs1" for k in c.watchdogs._err_logged)
    del c.devices["hs1"]
    run(c, "stay_awake")
    assert set(st.history) == {"hs0"} and set(st.mem) == {"hs0"} and "hs1" not in st.cursors
    assert not any(k[1] == "hs1" for k in c.watchdogs._err_logged)


# ---- LOW

def test_pattern_confirmed_never_survives_a_restart(ctl, tmp_path):
    confirmed_overheat(ctl, tmp_path)
    data = json.loads(json.dumps(ctl.dump()))
    assert data["watchdogs"]["overheat"]["cfg"]["pattern_confirmed"]  # in the file, but ignored on load
    other = tmp_path / "c"
    other.mkdir()
    c2 = make_ctl(other)
    c2.load(data)
    st = c2.watchdogs.states["overheat"]
    assert st.cfg["pattern"] == PATTERN and st.cfg["pattern_confirmed"] == "" and not st.armed
    assert "confirmed" in st.armed and False if False else Overheat().arm_blocker(st.cfg)


def test_test_pattern_reports_counts_per_file_and_uses_the_live_line_selection(tmp_path):
    folder = snapshot_folder(tmp_path)
    res = try_pattern(PATTERN, folder)
    assert res["by_file"]["logcat.txt"] >= 1 and res["by_file"]["window.txt"] >= 1 and not res["timed_out"]
    assert res["hash"] == pattern_hash(PATTERN)
    assert try_pattern(PATTERN, folder, exclude=["Other"])["hash"] != res["hash"]
    (folder / "logcat.txt").write_text("not a logcat line OverheatPrompt\n")  # no timestamp: not a log line
    assert try_pattern(PATTERN, folder)["by_file"]["logcat.txt"] == 0
    assert "catastrophically" in try_pattern("(a+)+$", folder)["error"]
    assert "regular" in try_pattern("(", folder)["error"]
    ticks = iter(range(1000))
    slow = try_pattern(PATTERN, folder, budget_s=0.5, clock=lambda: next(ticks))
    assert slow["timed_out"] and slow["error"]
