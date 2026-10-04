"""End-to-end: real server, simulated headsets with realistic imperfections."""

import asyncio
import base64
import dataclasses
import json
import statistics
import time

import aiohttp
import pytest

from conftest import make_mp4
from syncvr import controller as controller_mod
from syncvr.app import ServerConfig, SyncServer
from syncvr.controller import CommandError
from syncvr.sim import SimHeadset



async def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def server(content_dir, tmp_path):
    config = ServerConfig(content_dir=content_dir, data_dir=tmp_path / "data", host="127.0.0.1",
                          http_port=0, tcp_port=0, discovery=False)
    srv = SyncServer(config)
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
async def fleet(server, tmp_path):
    headsets, tasks = [], []

    async def start(count, **kw):
        for i in range(count):
            h = SimHeadset(len(headsets) + 1, "127.0.0.1", server.tcp_port,
                           download_dir=tmp_path / f"hs{len(headsets) + 1}", **kw)
            headsets.append(h)
            tasks.append(asyncio.create_task(h.run()))
        await wait_for(lambda: all(h.connected.is_set() for h in headsets))
        return headsets

    yield start
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def api(server, action, **params):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{server.http_port}/api/command",
                          json=dict(action=action, **params)) as r:
            body = await r.json()
            assert r.status == 200, body
            return body


def errors_ms(headsets):
    return [abs(h.true_error()) * 1000 for h in headsets if h.true_error() is not None]


async def test_headsets_register_and_report(server, fleet):
    headsets = await fleet(3)
    await wait_for(lambda: all(d.status for d in server.controller.devices.values()))
    devices = server.controller.devices
    assert set(devices) == {h.device_id for h in headsets}
    assert all(d.online for d in devices.values())
    # Clock offsets are arbitrary (thousands of seconds) but must be estimated precisely.
    for h in headsets:
        true_offset = time.monotonic() - h.clock()
        assert abs(h.sync.offset - true_offset) < 0.005


async def test_hello_reports_player_app(server, fleet):
    headsets = await fleet(1)
    dev = server.controller.devices[headsets[0].device_id]
    assert dev.player == "sim"
    # A native-player hello, and an old one that has no "player" field.
    for hello, expected in (({"player": "native"}, "native"), ({}, "")):
        base = {"device_id": "x1", "model": "Oculus Go", "app_version": "1", "serial": "s"}

        class Conn:
            remote_ip, http_port = "127.0.0.1", 0
            def send(self, *_): pass
            def close(self): pass

        server.controller.headset_connected({**base, **hello}, Conn())
        assert server.controller.devices["x1"].player == expected
        assert server.controller.devices["x1"].to_json()["player"] == expected
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{server.http_port}/api/state") as r:
                state = await r.json()
        assert {d["id"]: d["player"] for d in state["devices"]}["x1"] == expected


async def test_synchronized_play_pause_seek(server, fleet):
    headsets = await fleet(6)
    await api(server, "play", video="concert_360_TB.mp4")
    await wait_for(lambda: all(h.engine.state == "playing" for h in headsets), timeout=5)
    # First start on a fresh device: its start-up latency is still being learned.
    await asyncio.sleep(4.0)
    errs = errors_ms(headsets)
    assert len(errs) == 6
    assert max(errs) < 40, errs

    # Pause: everyone ends up paused on the same frame.
    await api(server, "pause")
    await wait_for(lambda: all(h.engine.state == "paused" and not h.player.is_seeking for h in headsets), timeout=3)
    positions = [h.player.time for h in headsets]
    assert max(positions) - min(positions) < 1e-6
    assert positions[0] > 3.0

    # Seek while paused, then resume from there.
    await api(server, "seek", pos=60.0)
    await wait_for(lambda: all(abs(h.player.time - 60.0) < 1e-6 for h in headsets), timeout=3)
    await api(server, "play")
    await wait_for(lambda: all(h.engine.state == "playing" for h in headsets), timeout=5)
    await asyncio.sleep(2.5)
    errs = errors_ms(headsets)
    assert max(errs) < 40, errs
    assert all(60.0 < h.player.time < 66.0 for h in headsets)

    # Seek while playing: new common anchor.
    await api(server, "seek", delta=-30.0)
    await asyncio.sleep(3.5)
    errs = errors_ms(headsets)
    assert max(errs) < 40, errs
    assert all(30.0 < h.player.time < 40.0 for h in headsets)

    await api(server, "stop")
    await wait_for(lambda: all(h.engine.state == "idle" for h in headsets), timeout=2)


async def test_late_joiner_catches_up(server, fleet):
    headsets = list(await fleet(3))
    await api(server, "play", video="concert_360_TB.mp4")
    await asyncio.sleep(2.5)
    # A headset that powers on mid-show is told to play and joins in sync.
    late = (await fleet(1))[-1]
    await api(server, "play", targets=[late.device_id])
    await asyncio.sleep(5.0)
    errs = errors_ms(headsets + [late])
    assert len(errs) == 4 and max(errs) < 40, errs
    assert late.engine.anchor["at"] == headsets[0].engine.anchor["at"]


async def test_reconnect_resumes_desired_state(server, fleet):
    headsets = await fleet(2)
    await api(server, "play", video="concert_360_TB.mp4")
    await asyncio.sleep(2.0)
    victim = headsets[0]
    victim.engine.on_stop()  # simulate an app restart losing playback
    victim.writer.close()  # ... and a dropped connection
    await wait_for(lambda: not server.controller.devices[victim.device_id].online, timeout=5)
    await wait_for(lambda: server.controller.devices[victim.device_id].online, timeout=10)
    await wait_for(lambda: victim.engine.state == "playing", timeout=5)
    await asyncio.sleep(2.5)
    errs = errors_ms(headsets)
    assert len(errs) == 2 and max(errs) < 40, errs


async def test_seek_only_mode(server, fleet):
    headsets = await fleet(3)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{server.http_port}/api/settings",
                          json={"correction_mode": "seek"}) as r:
            assert r.status == 200
    await wait_for(lambda: all(h.engine.settings["correction_mode"] == "seek" for h in headsets))
    await api(server, "play", video="trailer_flat.mp4")
    await asyncio.sleep(6.0)
    errs = errors_ms(headsets)
    assert len(errs) == 3 and max(errs) < 80, errs
    assert all(h.engine.rate == 1.0 for h in headsets)


async def test_content_distribution(server, fleet):
    server.controller.set_max_downloads(1)
    headsets = await fleet(3)
    await api(server, "sync_content", videos="all")
    lib = server.library.videos
    await wait_for(lambda: all(
        server.controller.devices[h.device_id].inventory == {n: v.size for n, v in lib.items()}
        for h in headsets), timeout=15)
    for h in headsets:
        for name, video in lib.items():
            assert (h.download_dir / name).read_bytes() == (server.library.root / name).read_bytes()
    assert not server.controller.distributor.active and not server.controller.distributor.queue

    # Deleting from one headset updates its inventory.
    await api(server, "delete_content", targets=[headsets[0].device_id], videos=["trailer_flat.mp4"])
    await wait_for(lambda: "trailer_flat.mp4" not in server.controller.devices[headsets[0].device_id].inventory)


async def test_download_resumes_partial_file(server, fleet, tmp_path):
    (tmp_path / "hs1").mkdir(parents=True, exist_ok=True)
    src = (server.library.root / "trailer_flat.mp4").read_bytes()
    (tmp_path / "hs1" / "trailer_flat.mp4.part").write_bytes(src[:1000])
    headsets = await fleet(1)
    await api(server, "sync_content", videos=["trailer_flat.mp4"])
    await wait_for(lambda: (headsets[0].download_dir / "trailer_flat.mp4").exists(), timeout=10)
    assert (headsets[0].download_dir / "trailer_flat.mp4").read_bytes() == src


async def test_dashboard_api(server, fleet):
    headsets = await fleet(2)
    base = f"http://127.0.0.1:{server.http_port}"
    async with aiohttp.ClientSession() as s:
        async with s.get(base + "/") as r:
            assert r.status == 200 and (await r.json())["name"] == "SyncVR API"
        async with s.get(base + "/ws") as r:
            assert r.status == 404
        async with s.get(base + "/static/app.js") as r:
            assert r.status == 404
        async with s.get(base + "/api/state") as r:
            state = await r.json()
        assert len(state["devices"]) == 2 and len(state["library"]) == 2

        dev_id = headsets[0].device_id
        async with s.post(f"{base}/api/devices/{dev_id}", json={"name": "Seat 1", "group": "Room A"}) as r:
            assert r.status == 200
        await wait_for(lambda: headsets[0].name == "Seat 1")

        async with s.post(f"{base}/api/command", json={"action": "play", "targets": {"group": "Room A"},
                                                     "video": "trailer_flat.mp4"}) as r:
            assert (await r.json())["result"]["targets"] == 1
        async with s.post(f"{base}/api/command", json={"action": "fly"}) as r:
            assert r.status == 400
        async with s.post(f"{base}/api/command", json={"action": "play", "video": "missing.mp4"}) as r:
            assert r.status == 400
        for bad in ({"action": "seek", "pos": "soon"}, {"action": "play", "targets": [{"x": 1}]},
                    {"action": "volume"}, {"action": "play", "targets": 42}):
            async with s.post(f"{base}/api/command", json=bad) as r:
                assert r.status == 400, bad

        async with s.post(f"{base}/api/library/trailer_flat.mp4", json={"rotation": 90}) as r:
            assert r.status == 200
        assert server.library.get("trailer_flat.mp4").rotation == 90

        async with s.post(base + "/api/command", json={"action": "volume", "value": 0.5}) as r:
            assert r.status == 200
        await wait_for(lambda: all(h.volume == 0.5 for h in headsets))

        # Range requests are what lets headsets resume downloads.
        async with s.get(base + "/content/trailer_flat.mp4", headers={"Range": "bytes=10-19"}) as r:
            assert r.status == 206 and len(await r.read()) == 10
        async with s.get(base + "/content/..%2Fstate.json") as r:
            assert r.status == 404


async def test_state_persists(content_dir, tmp_path):
    config = ServerConfig(content_dir=content_dir, data_dir=tmp_path / "data", host="127.0.0.1",
                          http_port=0, tcp_port=0, discovery=False)
    srv = SyncServer(config)
    await srv.start()
    h = SimHeadset(1, "127.0.0.1", srv.tcp_port, download_dir=tmp_path / "hs")
    task = asyncio.create_task(h.run())
    await wait_for(lambda: h.connected.is_set())
    srv.controller.update_device(h.device_id, {"name": "Front row"})
    srv.controller.update_settings({"deadband_ms": 33})
    srv.controller.update_video("concert_360_TB.mp4", {"stereo": "mono"})
    task.cancel()
    await srv.stop()

    srv2 = SyncServer(config)
    assert srv2.controller.devices[h.device_id].name == "Front row"
    assert srv2.controller.settings["deadband_ms"] == 33
    assert srv2.library.get("concert_360_TB.mp4").stereo == "mono"


async def test_password_protects_dashboard_not_content(content_dir, tmp_path):
    config = ServerConfig(content_dir=content_dir, data_dir=tmp_path / "data", host="127.0.0.1",
                          http_port=0, tcp_port=0, discovery=False, password="s3cret")
    srv = SyncServer(config)
    await srv.start()
    base = f"http://127.0.0.1:{srv.http_port}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/state") as r:
                assert r.status == 401
            auth = "Basic " + base64.b64encode(b"op:s3cret").decode()
            async with s.get(base + "/api/state", headers={"Authorization": auth}) as r:
                assert r.status == 200
            async with s.get(base + "/content/trailer_flat.mp4") as r:
                assert r.status == 200
    finally:
        await srv.stop()


async def test_discovery_beacon(content_dir, tmp_path):
    config = ServerConfig(content_dir=content_dir, data_dir=tmp_path / "data", host="127.0.0.1",
                          http_port=0, tcp_port=0, discovery_port=38766, broadcast=["127.0.0.1"])
    srv = SyncServer(config)
    await srv.start()
    try:
        h = SimHeadset(1, None, discovery_port=38766, download_dir=tmp_path / "hs")
        host, port = await asyncio.wait_for(h.discover(), 5)
        assert host == "127.0.0.1" and port == srv.tcp_port
    finally:
        await srv.stop()


async def test_many_headsets(server, fleet):
    headsets = await fleet(40)
    await api(server, "play", video="concert_360_TB.mp4")
    await asyncio.sleep(6.0)
    errs = errors_ms(headsets)
    assert len(errs) == 40
    assert statistics.median(errs) < 15 and max(errs) < 40, sorted(errs)[-5:]


async def test_headsets_verify_checksums(server, fleet):
    await asyncio.get_running_loop().run_in_executor(None, server.analyzer.wait, 20)
    lib = server.library
    name = "trailer_flat.mp4"
    digest = lib.sha256_of(name)
    assert digest
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{server.http_port}/content/{name}") as r:
            assert r.status == 200 and r.headers["X-Content-SHA256"] == digest
        async with s.get(f"http://127.0.0.1:{server.http_port}/api/state") as r:
            entry = next(v for v in (await r.json())["library"] if v["name"] == name)
            assert entry["sha256"] == digest and "issues" in entry

    good, bad = await fleet(2)
    await api(server, "sync_content", videos=[name], targets=[good.device_id])
    await wait_for(lambda: (good.download_dir / name).exists())

    # A server-side hash that does not match what is served makes the headset reject the file.
    server.analyzer._files[name]["sha256"] = "0" * 64
    assert lib.sha256_of(name) == "0" * 64
    await api(server, "sync_content", videos=[name], targets=[bad.device_id])
    await wait_for(lambda: not server.controller.distributor.active, timeout=15)
    assert not (bad.download_dir / name).exists()
    assert name not in server.controller.devices[bad.device_id].inventory


async def test_content_served_on_tcp_port(server):
    # The checksum header only exists once the background analyzer has hashed the file.
    await asyncio.get_running_loop().run_in_executor(None, server.analyzer.wait, 20)
    name = "trailer_flat.mp4"
    src = (server.library.root / name).read_bytes()
    base = f"http://127.0.0.1:{server.tcp_port}/content/{name}"
    async with aiohttp.ClientSession() as s:
        async with s.get(base) as r:
            assert r.status == 200 and await r.read() == src
            assert r.headers["X-Content-SHA256"] == server.library.sha256_of(name)
        async with s.head(base) as r:
            assert r.status == 200 and int(r.headers["Content-Length"]) == len(src)
        async with s.get(base, headers={"Range": "bytes=10-19"}) as r:
            assert r.status == 206 and await r.read() == src[10:20]
            assert r.headers["X-Content-SHA256"] == server.library.sha256_of(name)
        async with s.get(f"http://127.0.0.1:{server.tcp_port}/content/..%2Fstate.json") as r:
            assert r.status == 404
        async with s.get(f"http://127.0.0.1:{server.tcp_port}/api/state") as r:
            assert r.status == 404


async def test_silent_connection_times_out(server, monkeypatch):
    monkeypatch.setattr("syncvr.headset_server.HELLO_TIMEOUT_S", 0.3)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.tcp_port)
    assert await asyncio.wait_for(reader.read(), 3) == b""
    writer.close()


async def test_hello_in_one_byte_writes(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.tcp_port)
    for ch in b'{"type":"hello","device_id":"slow-1","name":"slow"}\n':
        writer.write(bytes([ch]))
        await writer.drain()
        await asyncio.sleep(0.005)
    await wait_for(lambda: "slow-1" in server.controller.devices
                   and server.controller.devices["slow-1"].conn is not None)
    writer.close()


async def test_manifest_url_uses_tcp_port(server, fleet):
    headsets = await fleet(1)
    await api(server, "sync_content", videos=["trailer_flat.mp4"])
    conn = server.controller.devices[headsets[0].device_id].conn
    assert conn.content_url("a b.mp4") == f"http://127.0.0.1:{server.tcp_port}/content/a%20b.mp4"
    assert str(server.http_port) != str(server.tcp_port)


async def test_stop_with_open_keepalive_http(content_dir, tmp_path):
    config = ServerConfig(content_dir=content_dir, data_dir=tmp_path / "data", host="127.0.0.1",
                          http_port=0, tcp_port=0, discovery=False)
    srv = SyncServer(config)
    await srv.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", srv.tcp_port)
    writer.write(b"GET /content/trailer_flat.mp4 HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n"
                 b"Range: bytes=0-9\r\n\r\n")
    await writer.drain()
    await reader.readuntil(b"\r\n\r\n")
    await asyncio.wait_for(srv.stop(), 5)
    writer.close()


async def test_command_warns_when_no_headset_is_online(server, fleet):
    # Known but offline headset: the command is accepted and the operator is told.
    ghost = server.controller.headset_connected(
        {"device_id": "ghost"}, type("C", (), {"remote_ip": "127.0.0.1", "http_port": 0, "send": lambda *a: None,
                                              "close": lambda *a: None})())
    server.controller.headset_disconnected(ghost, ghost.conn)
    result = (await api(server, "play", video="trailer_flat.mp4"))["result"]
    assert result["online"] == 0 and "online" in result["warning"]

    headsets = await fleet(1)
    result = (await api(server, "pause"))["result"]
    assert result["online"] == 1 and "warning" not in result
    result = (await api(server, "stop", targets=["ghost"]))["result"]
    assert result["online"] == 0 and result["warning"]
    # Only commands that drive playback warn.
    assert "warning" not in (await api(server, "volume", targets=["ghost"], value=0.5))["result"]

    with pytest.raises(CommandError):
        server.controller.execute("play", {"targets": []})
    with pytest.raises(CommandError):
        server.controller.execute("play", {"targets": {"group": "nobody"}, "video": "trailer_flat.mp4"})
    assert headsets


def restart_config(content_dir, tmp_path):
    return ServerConfig(content_dir=content_dir, data_dir=tmp_path / "data", host="127.0.0.1",
                        http_port=0, tcp_port=0, discovery=False)


async def test_download_resumes_after_server_restart(content_dir, tmp_path):
    make_mp4(content_dir / "big.mp4", duration=60.0, mdat_bytes=4_000_000)
    config = restart_config(content_dir, tmp_path)
    srv = SyncServer(config)
    await srv.start()
    h = SimHeadset(1, "127.0.0.1", srv.tcp_port, download_dir=tmp_path / "hs", throttle_bps=1_000_000)
    task = asyncio.create_task(h.run())
    srv2 = None
    try:
        await wait_for(lambda: h.connected.is_set())
        await api(srv, "sync_content", videos=["big.mp4"])
        part = tmp_path / "hs" / "big.mp4.part"
        await wait_for(lambda: part.exists() and part.stat().st_size > 100_000)
        tcp_port = srv.tcp_port
        # Ctrl+C mid-download must not hang on the in-flight transfer, and must keep the job.
        await asyncio.wait_for(srv.stop(), 5)
        saved = json.loads((tmp_path / "data" / "state.json").read_text())
        assert saved["downloads"][h.device_id]["files"] == ["big.mp4"]

        srv2 = SyncServer(dataclasses.replace(config, tcp_port=tcp_port))
        await srv2.start()
        await wait_for(lambda: (tmp_path / "hs" / "big.mp4").exists(), timeout=20)
        assert (tmp_path / "hs" / "big.mp4").read_bytes() == (content_dir / "big.mp4").read_bytes()
        assert len([m for m in h.messages if m["type"] == "sync_content"]) == 2
        await wait_for(lambda: not srv2.controller.distributor.active and not srv2.controller.distributor.idle)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if srv2 is not None:
            await srv2.stop()


async def test_idle_job_is_kept_until_headset_finishes(content_dir, tmp_path, monkeypatch):
    # The headset is busy hashing (no `download` in its status) for longer than the idle grace.
    monkeypatch.setattr(controller_mod, "DOWNLOAD_IDLE_GRACE_S", 0.5)
    srv = SyncServer(restart_config(content_dir, tmp_path))
    await srv.start()
    await asyncio.get_running_loop().run_in_executor(None, srv.analyzer.wait, 20)
    src = (content_dir / "trailer_flat.mp4").read_bytes()
    (tmp_path / "hs").mkdir()
    (tmp_path / "hs" / "trailer_flat.mp4").write_bytes(src[:-1] + b"X")  # same size, wrong content
    h = SimHeadset(1, "127.0.0.1", srv.tcp_port, download_dir=tmp_path / "hs")
    original = h._verify

    async def slow_verify(path, expected):
        await asyncio.sleep(2.0)
        return await original(path, expected)

    h._verify = slow_verify
    task = asyncio.create_task(h.run())
    try:
        await wait_for(lambda: h.connected.is_set())
        dist = srv.controller.distributor
        await wait_for(lambda: srv.controller.devices[h.device_id].inventory)
        srv.controller.devices[h.device_id].inventory = {}  # as if the file were not known to be there
        await api(srv, "sync_content", videos=["trailer_flat.mp4"])
        await wait_for(lambda: h.device_id in dist.idle, timeout=3)
        assert h.device_id not in dist.active
        assert h.device_id in srv.controller.dump()["downloads"]  # still survives a restart
        await wait_for(lambda: (tmp_path / "hs" / "trailer_flat.mp4").exists()
                       and (tmp_path / "hs" / "trailer_flat.mp4").read_bytes() == src, timeout=15)
        await wait_for(lambda: not dist.idle and not dist.active)
        assert srv.controller.dump()["downloads"] == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await srv.stop()


async def test_finish_from_an_older_job_is_ignored(content_dir, tmp_path):
    srv = SyncServer(restart_config(content_dir, tmp_path))
    await srv.start()
    h = SimHeadset(1, "127.0.0.1", srv.tcp_port, download_dir=tmp_path / "hs", throttle_bps=50_000)
    task = asyncio.create_task(h.run())
    try:
        await wait_for(lambda: h.connected.is_set())
        dist = srv.controller.distributor
        await api(srv, "sync_content", videos=["trailer_flat.mp4"])
        await wait_for(lambda: h.device_id in dist.active)
        job = dist.active[h.device_id]["id"]
        dev = srv.controller.devices[h.device_id]
        srv.controller.headset_message(dev, {"type": "downloads_finished", "ok": [], "failed": [], "job": "old"})
        assert h.device_id in dist.active
        # Old apps send no job id: accepted.
        srv.controller.headset_message(dev, {"type": "downloads_finished", "ok": [], "failed": []})
        assert h.device_id not in dist.active and job
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await srv.stop()
