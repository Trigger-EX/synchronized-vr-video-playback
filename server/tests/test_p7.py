"""P7: mass connect, purge, and the server side of the bandwidth test."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from conftest import make_mp4
from syncvr.adbtool import Adb
from syncvr.controller import BW_TIMEOUT_MSG, CommandError, Controller
from syncvr.fleetops import AdbFleet
from syncvr.library import Library
from test_fleetops import FAKE_ADB, adb_log, lines  # noqa: F401  (adb_log is a fixture)


def make_conn(ip="", sent=None):
    sent = [] if sent is None else sent
    return SimpleNamespace(remote_ip=ip, http_port=8080, send=sent.append, close=lambda: None,
                           content_url=lambda name: f"http://srv/content/{name}", sent=sent)


@pytest.fixture
def ctl(content_dir, adb_log, tmp_path):
    make_mp4(content_dir / "big.mp4", duration=10.0, mdat_bytes=2_000_000)
    lib = Library(content_dir)
    lib.scan()
    c = Controller(lib, fleet=AdbFleet(Adb(FAKE_ADB)))
    c.data_dir = tmp_path / "data"
    for i, (ip, serial) in enumerate([("192.168.1.20", "S1"), ("", "USB123"), ("192.168.1.20", "S3")]):
        d = c.headset_connected({"device_id": f"hs{i}", "serial": serial, "model": "Go"}, make_conn(ip))
        d.name = f"Go {i}"
    return c


def run(ctl, fn, settle=True):
    """Call ``fn()`` inside a running loop, then let jobs finish."""
    async def go():
        result = fn()
        for _ in range(300):
            await asyncio.sleep(0.02)
            if not settle or all(j["state"] != "running" for j in ctl.jobs.values()):
                break
        return result
    return asyncio.run(go())


def messages(ctl):
    return [e["message"] for e in ctl.events]


def test_connect_dedupes_and_notes_missing_addresses(ctl, adb_log):
    run(ctl, lambda: ctl.execute("connect", {"targets": "all"}))
    assert [l for l in lines(adb_log) if "connect" in l] == ["connect 192.168.1.20:5555"]
    job = next(iter(ctl.jobs.values()))
    by_id = {r["device"]: r for r in job["results"]}
    assert by_id["hs1"]["message"] == "no saved address" and not by_id["hs1"]["ok"]
    assert by_id["hs2"]["message"] == "same address as Go 0"
    assert by_id["192.168.1.20:5555"]["ok"]


def test_connect_token_follows_addresses_not_adb_listing(ctl):
    a = ctl.preview({"action": "connect", "targets": "all"}, listed=set())["token"]
    b = ctl.preview({"action": "connect", "targets": "all"}, listed={"S1"})["token"]
    assert a == b
    ctl.devices["hs1"].ip = "192.168.1.31"
    assert ctl.preview({"action": "connect", "targets": "all"})["token"] != a


def test_purge_requires_matching_token_and_tested_feature(ctl, adb_log):
    t = ["hs0"]
    p = ctl.preview({"action": "purge", "targets": t})
    assert p["needs_confirm"] and p["dry_run_choice"]
    assert p["tokens"]["dry_run"] == p["token"] and p["tokens"]["live"] != p["token"]
    assert any("Dry run" in w for w in p["warnings"])
    with pytest.raises(CommandError, match="not marked as tested"):
        ctl.execute("purge", {"targets": t, "confirm": p["token"]})
    ctl.set_feature_tested("adb.purge", True)
    with pytest.raises(CommandError, match="confirmation"):
        ctl.execute("purge", {"targets": t})
    with pytest.raises(CommandError, match="confirmation"):  # dry-run token cannot confirm a live run
        ctl.execute("purge", {"targets": t, "dry_run": False, "confirm": p["token"]})
    # the testing flag bypasses only the feature gate, not the token
    with pytest.raises(CommandError, match="confirmation"):
        ctl.execute("purge", {"targets": t, "testing": True})


def test_purge_dry_run_sends_nothing_and_live_reconnects(ctl, adb_log):
    ctl.set_feature_tested("adb.purge", True)
    t = {"targets": ["hs0"]}
    run(ctl, lambda: ctl.execute("purge", dict(t, confirm=ctl.preview(dict(t, action="purge"))["token"])))
    assert lines(adb_log) == []
    assert any("would disconnect and reconnect 192.168.1.20:5555" in m for m in messages(ctl))
    live = dict(t, dry_run=False)
    run(ctl, lambda: ctl.execute("purge", dict(live, confirm=ctl.preview(dict(live, action="purge"))["token"])))
    assert [l for l in lines(adb_log) if "connect" in l] == ["disconnect 192.168.1.20:5555",
                                                              "connect 192.168.1.20:5555"]


def test_purge_live_refused_during_push_and_running_adb_job(ctl, adb_log):
    ctl.set_feature_tested("adb.purge", True)
    live = {"targets": ["hs0"], "dry_run": False}
    token = ctl.preview(dict(live, action="purge"))["token"]
    from syncvr import automation
    lock = automation.push_lock_path(ctl.data_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}\n")
    with pytest.raises(CommandError, match="push is in progress"):
        run(ctl, lambda: ctl.execute("purge", dict(live, confirm=token)))
    lock.unlink()

    async def go():
        ctl.open_job("sleep", ["hs0"])  # a job that stays running
        with pytest.raises(CommandError, match="adb job is running"):
            ctl.execute("purge", dict(live, confirm=token))
    asyncio.run(go())
    assert lines(adb_log) == []


# ------------------------------------------------------------ bandwidth

BW = {"targets": "all", "testing": True}


def bw_sent(ctl, dev_id):
    return [m for m in ctl.devices[dev_id].conn.sent if m.get("type") == "bandwidth_test"]


def test_bandwidth_gated_until_tested(ctl):
    with pytest.raises(CommandError, match="not marked as tested"):
        run(ctl, lambda: ctl.execute("bandwidth_test", {"targets": "all"}))


def test_bandwidth_refused_in_show_mode_and_while_playing(ctl):
    ctl.set_show_mode(True)
    with pytest.raises(CommandError, match="Show Mode"):
        run(ctl, lambda: ctl.execute("bandwidth_test", BW), settle=False)
    ctl.set_show_mode(False)
    ctl.devices["hs1"].status = {"state": "playing"}
    with pytest.raises(CommandError, match="playing now: Go 1"):
        run(ctl, lambda: ctl.execute("bandwidth_test", BW), settle=False)
    assert not any(bw_sent(ctl, d) for d in ctl.devices)


def test_bandwidth_sends_url_and_refuses_duplicate(ctl):
    async def go():
        ctl.execute("bandwidth_test", dict(BW, targets=["hs0"], mb=1000, timeout_s=1))
        with pytest.raises(CommandError, match="already running on: Go 0"):
            ctl.execute("bandwidth_test", dict(BW, targets=["hs0"]))
    asyncio.run(go())
    (msg,) = bw_sent(ctl, "hs0")
    assert msg["url"] == "http://srv/content/big.mp4"
    assert msg["seconds"] == 5 and msg["bytes"] == ctl.library.videos["big.mp4"].size  # clamped, capped by size


def test_bandwidth_small_library_refused(content_dir, adb_log):
    lib = Library(content_dir)
    lib.scan()
    c = Controller(lib, fleet=AdbFleet(Adb(FAKE_ADB)))
    c.headset_connected({"device_id": "a", "serial": "S"}, make_conn())
    with pytest.raises(CommandError, match="smaller than 1 MB"):
        run(c, lambda: c.execute("bandwidth_test", {"targets": "all", "testing": True, "video": "trailer_flat.mp4"}),
            settle=False)


def start_bw(ctl, **params):
    job = ctl.execute("bandwidth_test", dict(BW, **params))["job"]
    return job


def result(ctl, dev_id, job, mbps, ok=True, **extra):
    ctl.headset_message(ctl.devices[dev_id], dict({"type": "bandwidth_result", "job": job, "ok": ok, "bytes": 1048576,
                                                    "seconds": 2.0, "mbps": mbps}, **extra))


def test_bandwidth_summary_median_and_min_with_failure(ctl):
    async def go():
        job = start_bw(ctl)
        result(ctl, "hs0", job, 80.0)
        result(ctl, "hs1", job, 20.0)
        result(ctl, "hs2", job, 0, ok=False, error="http 404")
        result(ctl, "hs2", "nope", 5.0)  # no longer pending: ignored
        return job
    asyncio.run(go())
    j = next(iter(ctl.jobs.values()))
    assert j["state"] == "done"
    assert j["summary"] == {"n": 2, "median": 50.0, "min": 20.0, "min_label": "Go 1", "failed": 1}
    assert ctl.devices["hs0"].bandwidth["mbps"] == 80.0
    assert ctl.devices["hs2"].bandwidth["error"] == "http 404"
    assert ctl.snapshot()["devices"][0]["bandwidth"]["ok"] is True
    assert any("median 50 Mbps" in m and "min 20 Mbps (Go 1)" in m for m in messages(ctl))


def test_bandwidth_unknown_job_ignored(ctl):
    async def go():
        job = start_bw(ctl, targets=["hs0"])
        result(ctl, "hs0", "wrong", 99.0)
        assert ctl._bw_pending["hs0"] == job and ctl.devices["hs0"].bandwidth is None
    asyncio.run(go())


def test_bandwidth_timeout_message(ctl):
    async def go():
        job = start_bw(ctl, targets=["hs0"])
        ctl._bw_timeout("hs0", job)
        return job
    job = asyncio.run(go())
    j = ctl.jobs[job]
    assert j["state"] == "failed" and j["results"][0]["message"] == BW_TIMEOUT_MSG
    assert "hs0" not in ctl._bw_pending


def test_bandwidth_headset_disconnect_fails_pending(ctl):
    async def go():
        job = start_bw(ctl, targets=["hs0"])
        dev = ctl.devices["hs0"]
        ctl.headset_disconnected(dev, dev.conn)
        return job
    job = asyncio.run(go())
    assert ctl.jobs[job]["results"][0]["message"] == "headset disconnected"
    assert not ctl._bw_pending


def test_bandwidth_parallel_queue_sends_in_waves(ctl):
    async def go():
        job = start_bw(ctl, parallel=1)
        sent = [len(bw_sent(ctl, d)) for d in ("hs0", "hs1", "hs2")]
        result(ctl, "hs0", job, 10.0)
        return sent, [len(bw_sent(ctl, d)) for d in ("hs0", "hs1", "hs2")]
    first, second = asyncio.run(go())
    assert first == [1, 0, 0] and second == [1, 1, 0]
