import asyncio
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncvr import diagnostics
from syncvr.adbtool import Adb
from syncvr.controller import CommandError, Controller
from syncvr.fleetops import AdbFleet
from syncvr.library import Library

FAKE_ADB = str(Path(__file__).parent / "fakeadb" / "adb")
NOW = datetime(2026, 10, 7, 12, 30, 45, 123456)


@pytest.fixture
def adb_log(tmp_path, monkeypatch):
    log = tmp_path / "adb.log"
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    return log


def logged(log):
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def fleet():
    return AdbFleet(Adb(FAKE_ADB))


def test_folder_layout_and_summary(fleet, tmp_path, adb_log):
    result = diagnostics.capture_device(fleet, "192.168.1.20:5555", "Go 1/A", tmp_path, screenshot=True, now=NOW)
    assert result.failed == [] and result.total == 12
    assert result.folder.parent == tmp_path / "snapshots"
    assert result.folder.name == "Go_1_A_192.168.1.20_5555_20261007-123045-123456"
    names = {p.name for p in result.folder.iterdir()}
    assert names == {f"{s.name}.txt" for s in diagnostics.STEPS} | {"screenshot.png", "SUMMARY.txt"}
    assert (result.folder / "screenshot.png").read_bytes() == (
        Path(__file__).parent / "fixtures" / "dumpsys" / "screencap.png").read_bytes()
    summary = (result.folder / "SUMMARY.txt").read_text()
    for line in ("Wakefulness: Awake", "Display state: ON", "Focused app: com.syncvr.player/.MainActivity",
                 "Focused window: com.syncvr.player/com.syncvr.player.MainActivity",
                 "Thermal status: 1 (light)", "Battery temperature: 31.2 C", "Commands: 12 of 12"):
        assert line in summary
    cmds = logged(adb_log)
    assert "-s 192.168.1.20:5555 shell logcat -d -t 2000" in cmds
    assert "-s 192.168.1.20:5555 exec-out screencap -p" in cmds
    # nothing written on the headset: only read-style commands
    assert not any(" push " in c or "> /sdcard" in c or "screencap -p /" in c for c in cmds)


def test_no_screenshot_by_default(fleet, tmp_path, adb_log):
    result = diagnostics.capture_device(fleet, "S1", "A", tmp_path, now=NOW)
    assert not (result.folder / "screenshot.png").exists()
    assert all("exec-out" not in c for c in logged(adb_log))


def test_same_timestamp_never_collides(fleet, tmp_path):
    a = diagnostics.capture_device(fleet, "S1", "A", tmp_path, now=NOW).folder
    b = diagnostics.capture_device(fleet, "S1", "A", tmp_path, now=NOW).folder
    c = diagnostics.capture_device(fleet, "S1", "A", tmp_path, now=NOW).folder
    assert len({a, b, c}) == 3 and b.name == a.name + "-2" and c.name == a.name + "-3"
    assert (a / "SUMMARY.txt").exists() and (b / "SUMMARY.txt").exists()


def test_safe_name_blocks_path_tricks():
    assert diagnostics.safe_name("../../etc/passwd") == "etc_passwd"
    assert diagnostics.safe_name("") == "x" and diagnostics.safe_name("..", "d") == "d"
    assert "/" not in diagnostics.safe_name("a/b\\c:d")


def test_partial_failure_writes_error_note_and_continues(fleet, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_FAIL_CMD", "audio screencap")
    result = diagnostics.capture_device(fleet, "S1", "A", tmp_path, screenshot=True, now=NOW)
    assert result.failed == ["audio", "screenshot"]
    assert "audio failed" in (result.folder / "audio.error.txt").read_text()
    assert not (result.folder / "audio.txt").exists() and not (result.folder / "screenshot.png").exists()
    assert (result.folder / "logcat.txt").exists() and (result.folder / "getprop.txt").exists()
    summary = (result.folder / "SUMMARY.txt").read_text()
    assert "Commands: 10 of 12" in summary and "Failed: audio, screenshot" in summary


def test_timeout_is_per_command(fleet, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_SLEEP_CMD", "logcat")
    monkeypatch.setenv("FAKE_ADB_SLEEP_CMD_S", "5")
    monkeypatch.setattr(diagnostics, "STEPS", tuple(
        s._replace(timeout_s=0.3 if s.name == "logcat" else 10.0) for s in diagnostics.STEPS))
    start = time.monotonic()
    result = diagnostics.capture_device(fleet, "S1", "A", tmp_path, now=NOW)
    assert time.monotonic() - start < 4
    assert result.failed == ["logcat"]
    assert "timed out" in (result.folder / "logcat.error.txt").read_text()
    assert (result.folder / "getprop.txt").exists()


def test_garbled_output_gives_unknowns_not_errors(tmp_path):
    class Garbage:
        adb = FAKE_ADB

        def shell(self, serial, command, **kw):
            return "\x00 ??? mWakefulness= Thermal Status: zz"
    result = diagnostics.capture_device(Garbage(), "S1", "A", tmp_path, now=NOW)
    summary = (result.folder / "SUMMARY.txt").read_text()
    assert result.failed == [] and "Wakefulness: unknown" in summary and "Thermal status: unknown" in summary


def test_concurrency_cap_is_four(tmp_path):
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class Slow:
        adb = FAKE_ADB

        def shell(self, serial, command, **kw):
            if command == "getprop":
                with lock:
                    state["now"] += 1
                    state["max"] = max(state["max"], state["now"])
                time.sleep(0.05)
                with lock:
                    state["now"] -= 1
            return "ok"
    threads = [threading.Thread(target=diagnostics.capture_device, args=(Slow(), f"S{i}", f"L{i}", tmp_path))
               for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] == diagnostics.MAX_CONCURRENT == 4
    assert len(list((tmp_path / "snapshots").iterdir())) == 12


# --------------------------------------------------------------- controller

@pytest.fixture
def ctl(content_dir, adb_log, tmp_path):
    lib = Library(content_dir)
    lib.scan()
    c = Controller(lib, fleet=AdbFleet(Adb(FAKE_ADB)))
    c.data_dir = tmp_path / "data"
    for i, (ip, serial) in enumerate([("192.168.1.20", "S1"), ("", "USB123"), ("10.9.9.9", "S3")]):
        d = c.headset_connected({"device_id": f"hs{i}", "serial": serial, "model": "Go"}, SimpleNamespace(
            remote_ip=ip, http_port=8080, send=lambda m: None, close=lambda: None))
        d.name = f"Go {i}"
    return c


def run(ctl, params):
    async def go():
        result = ctl.execute("snapshot", params)
        for _ in range(500):
            await asyncio.sleep(0.02)
            if all(j["state"] != "running" for j in ctl.jobs.values()):
                break
        return result
    return asyncio.run(go())


def test_snapshot_gated_until_tested_or_testing_flag(ctl):
    with pytest.raises(CommandError, match="not marked as tested"):
        ctl.execute("snapshot", {"targets": ["hs0"]})
    assert not (ctl.data_dir / "snapshots").exists()
    ctl.set_feature_tested("debug.snapshot", True)
    assert "job" in run(ctl, {"targets": ["hs0"]})  # no confirm token needed


def test_snapshot_job_reports_folder_per_headset_and_unreachable(ctl):
    assert ctl.preview({"action": "snapshot", "targets": ["hs0"]})["needs_confirm"] is False
    run(ctl, {"targets": ["hs0", "hs1", "hs2"], "testing": True, "screenshot": True})
    job = ctl.snapshot()["jobs"][0]
    by = {r["device"]: r for r in job["results"]}
    assert by["hs0"]["ok"] and by["hs1"]["ok"] and not by["hs2"]["ok"] and "unreachable" in by["hs2"]["message"]
    folders = sorted(p.name for p in (ctl.data_dir / "snapshots").iterdir())
    assert len(folders) == 2 and folders[0].startswith("Go_0_192.168.1.20_5555_")
    assert str(ctl.data_dir / "snapshots" / folders[0]) in by["hs0"]["message"]
    assert (ctl.data_dir / "snapshots" / folders[0] / "screenshot.png").exists()
    assert any(e["message"].startswith("snapshot: Go 0 OK (saved to ") for e in ctl.events)


def test_snapshot_partial_failure_is_ok_total_failure_is_failed(ctl, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_FAIL_CMD", "audio")
    run(ctl, {"targets": ["hs0"], "testing": True})
    r = ctl.snapshot()["jobs"][-1]["results"][0]
    assert r["ok"] and "1 of 11 commands failed: audio" in r["message"]
    monkeypatch.setenv("FAKE_ADB_FAIL", "192.168.1.20:5555")
    run(ctl, {"targets": ["hs0"], "testing": True})
    r = ctl.snapshot()["jobs"][-1]["results"][0]
    assert not r["ok"] and "every command failed" in r["message"]


def test_snapshot_bad_params(ctl):
    with pytest.raises(CommandError, match="screenshot"):
        ctl.execute("snapshot", {"targets": ["hs0"], "testing": True, "screenshot": "yes"})
    ctl.data_dir = None
    with pytest.raises(CommandError, match="data folder"):
        ctl.execute("snapshot", {"targets": ["hs0"], "testing": True})


def test_web_prefetches_adb_listing_for_snapshot(ctl):
    assert "snapshot" in ctl.adb_actions and "snapshot" not in ctl.confirm_actions
