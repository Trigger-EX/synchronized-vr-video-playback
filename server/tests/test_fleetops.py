import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncvr import features
from syncvr.adbtool import Adb
from syncvr.controller import CommandError, Controller
from syncvr.fleetops import MAX_WORKERS, AdbFleet, adb_serial, shared_executor
from syncvr.library import Library

FAKE_ADB = str(Path(__file__).parent / "fakeadb" / "adb")


@pytest.fixture
def adb_log(tmp_path, monkeypatch):
    log = tmp_path / "adb.log"
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    return log


def lines(log):
    return log.read_text().splitlines() if log.exists() else []


def dev(ip="", serial=""):
    return SimpleNamespace(ip=ip, serial=serial)


# ---------------------------------------------------------------- adb hardening

def test_adb_run_defaults(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    Adb("adb").run("S", "shell", "id")
    assert seen["stdin"] == subprocess.DEVNULL and seen["errors"] == "replace" and seen["timeout"] == 20
    Adb("adb").run("S", "push", "a", "b", timeout=None)
    assert seen["timeout"] is None
    Adb("adb").run("S", "install", "x", timeout=600)
    assert seen["timeout"] == 600


def test_adb_run_survives_invalid_utf8(tmp_path):
    script = tmp_path / "adb"
    script.write_text("#!/usr/bin/env bash\nprintf 'bad \\377\\376 bytes'\n")
    script.chmod(0o755)
    assert "bad" in Adb(str(script)).run("S", "shell", "x")


def test_adb_run_times_out(monkeypatch):
    monkeypatch.setenv("FAKE_ADB_SLEEP", "5")
    with pytest.raises(subprocess.TimeoutExpired):
        Adb(FAKE_ADB).run("S", "shell", "x", timeout=0.3)


# ----------------------------------------------------------------- fleet / serial

def test_fake_adb_logs_and_lists(adb_log):
    fleet = AdbFleet(Adb(FAKE_ADB))
    assert fleet.listed() == {"192.168.1.20:5555", "USB123"}  # offline entries are not usable
    assert lines(adb_log) == ["devices"]
    fleet.shell("USB123", "echo hi")
    assert lines(adb_log)[-1] == "-s USB123 shell echo hi"


def test_adb_serial_preference():
    listed = {"192.168.1.20:5555", "USB123"}
    assert adb_serial(dev("192.168.1.20", "USB123"), listed) == "192.168.1.20:5555"
    assert adb_serial(dev("10.9.9.9", "USB123"), listed) == "USB123"
    assert adb_serial(dev("", "USB123"), listed) == "USB123"
    assert adb_serial(dev("10.9.9.9", "NOPE"), listed) is None
    assert adb_serial(dev(), listed) is None


def test_fleet_listed_empty_when_adb_missing():
    assert AdbFleet(Adb("/nonexistent/adb")).listed() == set()


def test_shared_executor_is_capped_and_shared():
    assert shared_executor() is shared_executor()
    assert shared_executor()._max_workers == MAX_WORKERS == 10


# -------------------------------------------------------------------- features

def test_feature_helpers():
    assert features.mark_tested(set(), "power.sleep") == {"power.sleep"}
    assert features.unmark_tested({"power.sleep"}, "power.sleep") == set()
    assert features.normalize(["power.sleep", "gone.feature", 5]) == {"power.sleep"}
    assert features.is_tested({"power.sleep"}, "power.sleep")
    with pytest.raises(KeyError):
        features.mark_tested(set(), "nope")
    assert all(len(v) == 2 for v in features.REGISTRY.values())


# ----------------------------------------------- controller: features/preview/jobs

class FleetController(Controller):
    """Controller with a test-only gated, confirm-required adb action."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gated_actions["probe"] = "power.sleep"
        self.confirm_actions.add("probe")
        self.action_labels["probe"] = "Probe"

    def _act_probe(self, targets, params):
        items = [(d, s) for d, s in self.reachable(targets, params.get("_listed")) if s]
        return self.start_job("probe", items, lambda serial: self.fleet.shell(serial, "echo probe"))


@pytest.fixture
def ctl(content_dir, adb_log):
    lib = Library(content_dir)
    lib.scan()
    c = FleetController(lib, fleet=AdbFleet(Adb(FAKE_ADB)))
    for i, (ip, serial) in enumerate([("192.168.1.20", "S1"), ("", "USB123"), ("10.9.9.9", "S3")]):
        d = c.headset_connected({"device_id": f"hs{i}", "serial": serial, "model": "Go"}, SimpleNamespace(
            remote_ip=ip, http_port=8080, send=lambda m: None, close=lambda: None))
        d.name = f"Go {i}"
    return c


def test_saved_includes_ip_and_tested_features_roundtrip(ctl, content_dir):
    assert ctl.devices["hs0"].saved()["ip"] == "192.168.1.20"
    ctl.set_feature_tested("power.sleep", True)
    data = ctl.dump()
    assert data["tested_features"] == ["power.sleep"]
    data["tested_features"].append("removed.feature")
    lib = Library(content_dir)
    lib.scan()
    c2 = Controller(lib)
    c2.load(data)
    assert c2.tested_features == {"power.sleep"}
    assert c2.devices["hs0"].ip == "192.168.1.20"
    snap = c2.snapshot()
    assert {f["key"]: f["tested"] for f in snap["features"]}["power.sleep"] is True
    assert snap["jobs"] == []


def test_set_feature_tested_rejects_unknown(ctl):
    with pytest.raises(CommandError):
        ctl.set_feature_tested("nope", True)
    ctl.set_feature_tested("power.sleep", True)
    ctl.set_feature_tested("power.sleep", False)
    assert ctl.tested_features == set() and ctl.dirty


def test_preview_scope_and_token(ctl):
    p = ctl.preview({"action": "probe", "targets": ["hs0", "hs1"]})
    assert p["scope_text"] == "Probe 2 headsets: Go 0, Go 1"
    assert p["labels"] == ["Go 0", "Go 1"] and p["needs_confirm"] is True
    every = ctl.preview({"action": "probe", "targets": "all"})
    assert every["scope_text"] == "Probe EVERY headset (3)"
    assert ctl.preview({"action": "probe", "targets": ["hs1", "hs0"]})["token"] == p["token"]
    assert ctl.preview({"action": "probe", "targets": ["hs0", "hs1"], "dry_run": False})["token"] != p["token"]
    assert every["token"] != p["token"]
    with pytest.raises(CommandError):
        ctl.preview({"action": "nope"})
    assert ctl.preview({"action": "identify", "targets": ["hs0"]})["needs_confirm"] is False


def test_gate_refuses_untested_unless_testing(ctl):
    token = ctl.preview({"action": "probe", "targets": ["hs0"]})["token"]
    with pytest.raises(CommandError, match="not marked as tested"):
        ctl.execute("probe", {"targets": ["hs0"], "confirm": token})
    ctl.set_feature_tested("power.sleep", True)
    assert "job" in ctl_run(ctl, {"targets": ["hs0"], "confirm": token})


def ctl_run(ctl, params):
    async def go():
        result = ctl.execute("probe", params)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if all(j["state"] != "running" for j in ctl.jobs.values()):
                break
        return result
    return asyncio.run(go())


def test_testing_flag_bypasses_gate(ctl):
    token = ctl.preview({"action": "probe", "targets": ["hs0"]})["token"]
    assert "job" in ctl_run(ctl, {"targets": ["hs0"], "confirm": token, "testing": True})


def test_token_mismatch_and_missing_refused(ctl):
    ctl.set_feature_tested("power.sleep", True)
    token = ctl.preview({"action": "probe", "targets": ["hs0"]})["token"]
    with pytest.raises(CommandError, match="confirmation"):
        ctl.execute("probe", {"targets": ["hs0"]})
    with pytest.raises(CommandError, match="confirmation"):
        ctl.execute("probe", {"targets": ["hs0", "hs1"], "confirm": token})  # scope changed
    with pytest.raises(CommandError, match="confirmation"):
        ctl.execute("probe", {"targets": ["hs0"], "confirm": token, "dry_run": False})


def test_token_changes_when_adb_serials_change(ctl):
    a = ctl.preview({"action": "probe", "targets": ["hs0"]}, listed={"192.168.1.20:5555"})["token"]
    b = ctl.preview({"action": "probe", "targets": ["hs0"]}, listed={"S1"})["token"]
    assert a != b


def test_job_results_marshalled_to_loop_thread(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_FAIL", "USB123")
    ctl.set_feature_tested("power.sleep", True)
    token = ctl.preview({"action": "probe", "targets": ["hs0", "hs1", "hs2"]})["token"]
    threads = []
    orig = ctl.log_event
    import threading
    ctl.log_event = lambda *a, **k: (threads.append(threading.get_ident()), orig(*a, **k))

    async def go():
        main = threading.get_ident()
        result = ctl.execute("probe", {"targets": ["hs0", "hs1", "hs2"], "confirm": token})
        assert set(result) >= {"job"} and ctl.jobs[result["job"]]["state"] == "running"
        for _ in range(200):
            await asyncio.sleep(0.02)
            if ctl.jobs[result["job"]]["state"] != "running":
                break
        return main, result["job"]

    main, job_id = asyncio.run(go())
    assert threads and set(threads) == {main}
    job = ctl.snapshot()["jobs"][0]
    assert job["id"] == job_id and job["total"] == 2 and job["done"] == 2 and job["failed"] == 1
    msgs = [e["message"] for e in ctl.events]
    assert any(m.startswith("probe: Go 0 OK") for m in msgs)
    assert any(m.startswith("probe: Go 1 FAILED") for m in msgs)
    assert sum("-s " in line and "echo probe" in line for line in lines(adb_log)) == 2


def test_start_job_without_loop_is_an_error(ctl):
    with pytest.raises(CommandError):
        ctl.start_job("x", [], lambda s: "")


def test_web_preview_and_feature_routes(ctl):
    from aiohttp.test_utils import TestClient, TestServer
    from syncvr.web import WebApp

    async def go():
        async with TestClient(TestServer(WebApp(ctl).app)) as client:
            r = await client.post("/api/command/preview", json={"action": "probe", "targets": ["hs0"]})
            preview = await r.json()
            assert r.status == 200 and preview["needs_confirm"] and preview["token"]
            assert (await client.post("/api/command/preview", json={"action": "nope"})).status == 400
            r = await client.post("/api/command", json={"action": "probe", "targets": ["hs0"]})
            assert r.status == 400 and "not marked as tested" in (await r.json())["error"]
            assert (await client.post("/api/features/power.sleep")).status == 200
            assert "power.sleep" in ctl.tested_features
            r = await client.post("/api/command", json={"action": "probe", "targets": ["hs0"],
                                                         "confirm": preview["token"]})
            assert r.status == 200 and "job" in (await r.json())["result"]
            assert (await client.delete("/api/features/power.sleep")).status == 200
            assert "power.sleep" not in ctl.tested_features
            assert (await client.post("/api/features/bogus")).status == 400
    asyncio.run(go())


# ------------------------------------------------------------------ P1: power

@pytest.fixture
def wake_state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("FAKE_ADB_STATE", str(d))
    return d


def fast_fleet():
    fleet = AdbFleet(Adb(FAKE_ADB))
    fleet.sleeper = staticmethod(lambda s: None)
    return fleet


def test_fake_adb_dumpsys_power(adb_log, wake_state, monkeypatch):
    fleet = AdbFleet(Adb(FAKE_ADB))
    assert fleet.wakefulness("S1") == "Awake"
    fleet.shell("S1", "input keyevent 223")
    assert fleet.wakefulness("S1") == "Asleep"
    assert "mWakefulness=Asleep" in fleet.shell("S1", "dumpsys power")
    monkeypatch.setenv("FAKE_ADB_STUCK", "S1")
    fleet.shell("S1", "input keyevent 224")
    assert fleet.wakefulness("S1") == "Asleep"


def test_sleep_wake_send_keyevents(adb_log):
    fleet = AdbFleet(Adb(FAKE_ADB))
    fleet.sleep_screen("S1")
    fleet.wake_screen("S1")
    assert lines(adb_log) == ["-s S1 shell input keyevent 223", "-s S1 shell input keyevent 224"]


def test_screen_refresh_sequence(adb_log, wake_state):
    sleeps = []
    fleet = AdbFleet(Adb(FAKE_ADB))
    fleet.sleeper = staticmethod(sleeps.append)
    assert fleet.screen_refresh("S1", min_asleep_s=1.5) == "refreshed"
    cmds = [l.split(" shell ", 1)[1] for l in lines(adb_log)]
    assert cmds[0] == "input keyevent 223" and cmds[-2] == "input keyevent 224"
    assert cmds.count("input keyevent 223") == 1 and cmds.count("input keyevent 224") == 1
    assert cmds[1:-2] == ["dumpsys power | grep mWakefulness"] * (len(cmds) - 3)
    assert 1.5 in sleeps  # held asleep for min_asleep_s before the wake
    assert fleet.wakefulness("S1") == "Awake"


def test_screen_refresh_fails_when_never_asleep(adb_log, wake_state, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_STUCK", "S1")
    ticks = iter(range(0, 1000))
    fleet = AdbFleet(Adb(FAKE_ADB))
    fleet.sleeper = staticmethod(lambda s: None)
    fleet.clock = staticmethod(lambda: next(ticks) * 0.5)
    with pytest.raises(RuntimeError, match="did not go to sleep within 3 s"):
        fleet.screen_refresh("S1")
    assert "input keyevent 224" not in " ".join(lines(adb_log))  # never woke a screen that did not sleep


def test_poweroff_dry_run_sends_nothing(adb_log):
    fleet = AdbFleet(Adb(FAKE_ADB))
    assert "would power off" in fleet.poweroff("S1")
    assert lines(adb_log) == []
    fleet.poweroff("S1", dry_run=False)
    assert lines(adb_log) == ["-s S1 shell reboot -p"]


@pytest.fixture
def pctl(ctl):
    return ctl  # three headsets: S1 via ip, USB123 via usb, S3 (10.9.9.9, unreachable)


def run_action(ctl, action, params):
    async def go():
        result = ctl.execute(action, params)
        for _ in range(300):
            await asyncio.sleep(0.02)
            if all(j["state"] != "running" for j in ctl.jobs.values()):
                break
        return result
    return asyncio.run(go())


def confirm(ctl, action, params):
    return ctl.preview(dict(params, action=action))["token"]


def test_sleep_wake_jobs_and_intentionally_asleep(pctl, adb_log, wake_state):
    t = ["hs0", "hs1"]
    assert pctl.preview({"action": "sleep", "targets": t})["scope_text"] == "Sleep 2 headsets: Go 0, Go 1"
    assert pctl.preview({"action": "sleep", "targets": "all"})["scope_text"] == "Sleep EVERY headset (3)"
    with pytest.raises(CommandError, match="confirmation"):
        pctl.execute("sleep", {"targets": t})
    run_action(pctl, "sleep", {"targets": t, "confirm": confirm(pctl, "sleep", {"targets": t})})
    assert pctl.snapshot()["asleep"] == ["hs0", "hs1"]
    assert sum(l.endswith("input keyevent 223") for l in lines(adb_log)) == 2
    run_action(pctl, "wake", {"targets": ["hs0"], "confirm": confirm(pctl, "wake", {"targets": ["hs0"]})})
    assert pctl.snapshot()["asleep"] == ["hs1"]
    run_action(pctl, "screen_refresh", {"targets": ["hs1"], "min_asleep_s": 0,
                                        "confirm": confirm(pctl, "screen_refresh", {"targets": ["hs1"]})})
    assert pctl.snapshot()["asleep"] == []
    assert pctl.fleet.wakefulness("USB123") == "Awake"


def test_unreachable_headset_reported_and_not_marked_asleep(pctl, adb_log):
    t = ["hs2"]
    run_action(pctl, "sleep", {"targets": t, "confirm": confirm(pctl, "sleep", {"targets": t})})
    assert pctl.snapshot()["asleep"] == []
    job = pctl.snapshot()["jobs"][0]
    assert job["failed"] == 1 and job["state"] == "failed"
    assert any(e["message"].startswith("sleep: Go 2 FAILED (unreachable") for e in pctl.events)
    assert all("keyevent" not in l for l in lines(adb_log))
    assert any("not reachable by adb" in w for w in pctl.preview({"action": "sleep", "targets": t})["warnings"])


def test_failed_sleep_clears_asleep_mark(pctl, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_FAIL", "USB123")
    t = ["hs1"]
    run_action(pctl, "sleep", {"targets": t, "confirm": confirm(pctl, "sleep", {"targets": t})})
    assert pctl.intentionally_asleep == set()


def test_refresh_preview_warns_for_playing_headset(pctl):
    assert pctl.preview({"action": "screen_refresh", "targets": ["hs0"]})["warnings"] == []
    pctl.devices["hs0"].status = {"state": "playing"}
    warnings = pctl.preview({"action": "screen_refresh", "targets": ["hs0", "hs1"]})["warnings"]
    assert len(warnings) == 1 and "interrupts playback" in warnings[0] and "Go 0" in warnings[0]
    assert pctl.preview({"action": "sleep", "targets": ["hs0"]})["warnings"] == []


def test_poweroff_gated_dry_run_default_and_every_guard(pctl, adb_log):
    t = ["hs0", "hs1"]
    p = pctl.preview({"action": "poweroff", "targets": t})
    assert p["scope_text"] == "Power off 2 headsets: Go 0, Go 1" and p["needs_confirm"] and not p["every"]
    assert any("Dry run" in w for w in p["warnings"])
    with pytest.raises(CommandError, match="not marked as tested"):
        pctl.execute("poweroff", {"targets": t, "confirm": p["token"]})
    pctl.set_feature_tested("power.poweroff", True)
    with pytest.raises(CommandError, match="confirmation"):
        pctl.execute("poweroff", {"targets": t})
    run_action(pctl, "poweroff", {"targets": t, "confirm": p["token"]})  # dry_run defaults to true
    assert not any("reboot" in l for l in lines(adb_log))
    msgs = [e["message"] for e in pctl.events]
    assert any(m.startswith("poweroff dry run: would power off 2 headsets") for m in msgs)
    assert any("poweroff: Go 0 OK (would power off" in m for m in msgs)

    real = {"targets": t, "dry_run": False}
    run_action(pctl, "poweroff", dict(real, confirm=confirm(pctl, "poweroff", real)))
    assert sorted(l for l in lines(adb_log) if "reboot" in l) == [
        "-s 192.168.1.20:5555 shell reboot -p", "-s USB123 shell reboot -p"]

    for spec in ("all", None, ["hs0", "hs1", "hs2"]):
        params = {"dry_run": False} if spec is None else {"targets": spec, "dry_run": False}
        token = pctl.preview(dict(params, action="poweroff"))["token"]
        with pytest.raises(CommandError, match="EVERY"):
            pctl.execute("poweroff", dict(params, confirm=token))
    every = {"targets": "all", "dry_run": False}
    before = len([l for l in lines(adb_log) if "reboot" in l])
    run_action(pctl, "poweroff", dict(every, confirm=confirm(pctl, "poweroff", every), confirm_every=True))
    assert len([l for l in lines(adb_log) if "reboot" in l]) == before + 2  # S3 unreachable


def test_poweroff_every_dry_run_allowed(pctl, adb_log):
    p = {"targets": "all"}
    run_action(pctl, "poweroff", dict(p, testing=True, confirm=confirm(pctl, "poweroff", p)))
    assert not any("reboot" in l for l in lines(adb_log))


def test_adbtool_cli_sleep_wake_refresh(adb_log, wake_state, monkeypatch, capsys):
    import argparse
    from syncvr import adbtool
    monkeypatch.setattr("syncvr.fleetops.AdbFleet.sleeper", staticmethod(lambda s: None))

    def cli(*argv):
        p = argparse.ArgumentParser()
        adbtool.add_arguments(p)
        return adbtool.run(p.parse_args(["--adb", FAKE_ADB, "-s", "S1", *argv]))
    assert cli("sleep") == 0 and AdbFleet(Adb(FAKE_ADB)).wakefulness("S1") == "Asleep"
    assert cli("wake") == 0 and AdbFleet(Adb(FAKE_ADB)).wakefulness("S1") == "Awake"
    assert cli("refresh", "--min-asleep", "0") == 0
    assert capsys.readouterr().out.count("[ok ]") == 3


def test_resolve_serials_refuses_shared_addresses_and_prefers_own_serial():
    from syncvr.fleetops import resolve_serials

    def d(i, ip, serial):
        return SimpleNamespace(device_id=i, ip=ip, serial=serial)
    listed = {"10.0.0.1:5555", "10.0.0.2:5555", "SER9"}
    serials, notes = resolve_serials([d("a", "10.0.0.1", "x"), d("b", "10.0.0.2", "SER9"), d("c", "10.0.0.2", "y")],
                                     listed)
    assert serials == {"a": "10.0.0.1:5555", "b": "SER9", "c": None}
    assert set(notes) == {"b", "c"} and "claimed by 2" in notes["c"]
    serials, notes = resolve_serials([d("a", "10.0.0.1", ""), d("b", "10.0.0.1", "")], listed)
    assert serials == {"a": None, "b": None} and len(notes) == 2
    assert resolve_serials([d("a", "10.0.0.1", "")], set()) == ({"a": None}, {})


# ------------------------------------------------------- P7: disconnect, scan

def test_scan_candidates_limits():
    from syncvr.fleetops import scan_candidates
    hosts = scan_candidates("192.168.1.0/27")
    assert len(hosts) == 30 and hosts[0] == "192.168.1.1" and hosts[-1] == "192.168.1.30"
    assert scan_candidates("192.168.1.77/32") == ["192.168.1.77"]
    assert scan_candidates("192.168.1.5/27")[0] == "192.168.1.1"  # strict=False
    for bad, text in (("192.168.1.0/26", "limit"), ("8.8.8.0/28", "private"), ("nonsense", "valid"),
                      ("10.0.0.0/8", "limit"), ("fd00::/126", "IPv4")):
        with pytest.raises(ValueError, match=text):
            scan_candidates(bad)


def test_reconnect_argv_order_and_dry_run(adb_log, monkeypatch):
    fleet = AdbFleet(Adb(FAKE_ADB))
    pauses = []
    monkeypatch.setattr(AdbFleet, "sleeper", staticmethod(pauses.append))
    assert "dry run" in fleet.reconnect("10.0.0.5:5555", dry_run=True)
    assert lines(adb_log) == []
    fleet.reconnect("10.0.0.5:5555", dry_run=False)
    assert lines(adb_log) == ["disconnect 10.0.0.5:5555", "connect 10.0.0.5:5555"]
    assert pauses == [0.5]


def test_disconnect_failure_raises_and_stops_reconnect(adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_DISCONNECT_FAIL", "10.0.0.5:5555")
    fleet = AdbFleet(Adb(FAKE_ADB))
    with pytest.raises(RuntimeError, match="no such device"):
        fleet.disconnect("10.0.0.5:5555")
    with pytest.raises(RuntimeError):
        fleet.reconnect("10.0.0.5:5555", dry_run=False)
    assert "connect 10.0.0.5:5555" not in lines(adb_log)


def test_probe_open_and_closed_port():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    fleet = AdbFleet(Adb(FAKE_ADB))
    try:
        assert fleet.probe("127.0.0.1", port, timeout=1.0) is True
    finally:
        srv.close()
    assert fleet.probe("127.0.0.1", port, timeout=0.2) is False
