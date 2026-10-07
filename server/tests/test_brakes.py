"""Show Mode and brakes (automation.py, Controller.brake, /api/show_mode, push lock, topbar)."""

import asyncio
import os
import subprocess
import sys

import pytest

from syncvr import automation
from syncvr.controller import CommandError

from test_controller_state import connect, make_controller


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_show_mode_persists_and_defaults_off(content_dir):
    c = make_controller(content_dir)
    assert c.show_mode is False and c.brake() is None
    c.dirty = False
    c.set_show_mode(True)
    assert c.dirty and c.brake() == "show_mode"
    state = c.dump()
    assert state["show_mode"] is True
    c2 = make_controller(content_dir, state)
    assert c2.show_mode is True and c2.brake() == "show_mode"
    c2.set_show_mode(False)
    assert make_controller(content_dir, c2.dump()).show_mode is False
    assert make_controller(content_dir, {"show_mode": "yes"}).show_mode is False
    with pytest.raises(CommandError):
        c.set_show_mode("true")


def test_snapshot_brake_field(content_dir):
    c = make_controller(content_dir)
    assert c.snapshot()["brake"] == {"active": False, "reason": None, "show_mode": False}
    c.set_show_mode(True)
    assert c.snapshot()["brake"] == {"active": True, "reason": "show_mode", "show_mode": True}


def test_brake_while_downloads_active(content_dir):
    c = make_controller(content_dir)
    connect(c)
    c.execute("sync_content", {"targets": ["hs1"]})
    assert "hs1" in c.distributor.active
    assert c.brake() == "sync in progress"
    c.set_show_mode(True)
    assert c.brake() == "show_mode"  # show mode outranks
    c.set_show_mode(False)
    c.execute("cancel_downloads", {"targets": ["hs1"]})
    assert c.brake() is None


def test_push_lock_live_pid_brakes(content_dir, tmp_path):
    c = make_controller(content_dir)
    c.data_dir = tmp_path
    assert c.brake() is None
    (tmp_path / automation.PUSH_LOCK_FILE).write_text(f"{os.getpid()}\n")
    assert c.brake() == "push in progress"
    assert c.snapshot()["brake"]["reason"] == "push in progress"


@pytest.mark.parametrize("content", [None, "", "garbage", "-5", "0", "99999999999999999999"])
def test_stale_or_unreadable_lock_never_brakes(content_dir, tmp_path, content):
    c = make_controller(content_dir)
    c.data_dir = tmp_path
    lock = tmp_path / automation.PUSH_LOCK_FILE
    if content is None:
        lock.write_text(str(dead_pid()))
    else:
        lock.write_text(content)
    assert c.brake() is None


def test_lock_is_a_directory_or_binary_does_not_brake(tmp_path):
    (tmp_path / automation.PUSH_LOCK_FILE).mkdir()
    assert automation.push_in_progress(tmp_path) is False
    (tmp_path / automation.PUSH_LOCK_FILE).rmdir()
    (tmp_path / automation.PUSH_LOCK_FILE).write_bytes(b"\xff\xfe\x00")
    assert automation.push_in_progress(tmp_path) is False
    assert automation.push_in_progress(None) is False


def test_push_lock_context_writes_and_cleans_up(tmp_path):
    lock = tmp_path / "sub" / automation.PUSH_LOCK_FILE
    with automation.push_lock(tmp_path / "sub"):
        assert lock.read_text().strip() == str(os.getpid())
        assert automation.push_in_progress(tmp_path / "sub")
    assert not lock.exists()
    with pytest.raises(RuntimeError):
        with automation.push_lock(tmp_path / "sub"):
            raise RuntimeError("boom")
    assert not lock.exists()


def test_push_lock_unwritable_dir_still_runs(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with automation.push_lock(blocker / "data"):
        pass


def test_adb_push_cli_holds_lock(tmp_path, monkeypatch):
    from syncvr import __main__ as cli
    fake = os.path.join(os.path.dirname(__file__), "fakeadb", "adb")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    seen = []
    real = automation.push_in_progress

    from syncvr import adbtool
    orig = adbtool.for_each

    def spy(serials, fn, workers=16):
        seen.append(real(tmp_path))
        return orig(serials, fn, workers)
    monkeypatch.setattr(adbtool, "for_each", spy)
    monkeypatch.setenv("FAKE_ADB_LOG", str(tmp_path / "log"))
    cli.main(["adb", "--adb", fake, "--data", str(tmp_path), "-s", "S1", "push", str(video)])
    assert seen == [True]
    assert not (tmp_path / automation.PUSH_LOCK_FILE).exists()


def test_preview_warns_when_show_mode_on(content_dir):
    c = make_controller(content_dir)
    connect(c)
    assert c.preview({"action": "sleep", "targets": ["hs1"]}, listed=set())["show_mode"] is False
    c.set_show_mode(True)
    p = c.preview({"action": "sleep", "targets": ["hs1"]}, listed=set())
    assert p["show_mode"] and automation.SHOW_MODE_WARNING in p["warnings"]
    assert "Show Mode is on" in automation.SHOW_MODE_WARNING


def test_manual_command_still_runs_in_show_mode(content_dir):
    c = make_controller(content_dir)
    conn = connect(c)
    c.set_show_mode(True)
    c.execute("sync_content", {"targets": ["hs1"]})
    assert any(m["type"] == "sync_content" for m in conn.sent)


def test_web_show_mode_route(content_dir):
    from aiohttp.test_utils import TestClient, TestServer
    from syncvr.web import WebApp
    c = make_controller(content_dir)

    async def go():
        async with TestClient(TestServer(WebApp(c).app)) as client:
            r = await client.post("/api/show_mode", json={"enabled": True})
            assert r.status == 200
            assert (await r.json())["brake"] == {"active": True, "reason": "show_mode", "show_mode": True}
            assert c.show_mode and c.dirty
            assert (await (await client.get("/api/state")).json())["brake"]["show_mode"] is True
            assert (await client.post("/api/show_mode", json={"enabled": "yes"})).status == 400
            assert (await client.post("/api/show_mode", json={})).status == 400
            assert (await client.post("/api/show_mode", data="nope")).status == 400
            assert (await client.post("/api/show_mode", json={"enabled": False})).status == 200
            assert c.show_mode is False
    asyncio.run(go())
