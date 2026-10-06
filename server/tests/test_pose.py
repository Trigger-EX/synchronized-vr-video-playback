import asyncio

import pytest

from syncvr.controller import Controller, POSE_RENEW_S
from syncvr.library import Library
from syncvr.sim import SimHeadset


class Conn:
    remote_ip = "10.0.0.2"
    http_port = 8080

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass


def make(content_dir):
    lib = Library(content_dir)
    lib.scan()
    c = Controller(lib)
    conn = Conn()
    dev = c.headset_connected({"device_id": "hs1"}, conn)
    conn.sent.clear()
    return c, dev, conn


def streams(conn):
    return [m["hz"] for m in conn.sent if m["type"] == "pose_stream"]


def test_pose_stored_without_changed(content_dir):
    c, dev, _ = make(content_dir)
    calls = []
    c.add_listener(lambda: calls.append(1))
    c.headset_message(dev, {"type": "pose", "yaw": 10, "pitch": -5, "roll": 1})
    assert calls == []
    assert c.pose_of("hs1")["yaw"] == 10.0 and "t" in c.pose_of("hs1")
    assert "pose" not in dev.to_json()
    c.headset_message(dev, {"type": "pose", "yaw": "bad"})  # ignored
    assert c.pose_of("hs1")["yaw"] == 10.0
    assert c.pose_of("nope") is None


def test_follow_unfollow(content_dir):
    c, dev, conn = make(content_dir)
    c.follow("hs1")
    assert streams(conn) == [10]
    c.headset_message(dev, {"type": "pose", "yaw": 1, "pitch": 2, "roll": 3})
    c.unfollow()
    assert streams(conn) == [10, 0]
    assert c.pose_of("hs1") is None and c.following is None


def test_follow_resent_on_reconnect(content_dir):
    c, dev, conn = make(content_dir)
    c.follow("hs1")
    c.headset_disconnected(dev, conn)
    conn2 = Conn()
    c.headset_connected({"device_id": "hs1"}, conn2)
    assert streams(conn2) == [10]


def test_follow_renews_on_loop(content_dir, monkeypatch):
    monkeypatch.setattr("syncvr.controller.POSE_RENEW_S", 0.05)

    async def run():
        c, dev, conn = make(content_dir)
        c.follow("hs1")
        await asyncio.sleep(0.2)
        n = len(streams(conn))
        c.unfollow()
        await asyncio.sleep(0.15)
        return n, streams(conn)

    n, final = asyncio.run(run())
    assert n >= 3
    assert final[-1] == 0 and final.count(0) == 1
    assert POSE_RENEW_S == 10.0


def test_sim_answers_pose_stream():
    sim = SimHeadset(0, None, realistic=False)
    sent = []
    sim.send = sent.append
    sim._handle({"type": "pose_stream", "hz": 10}, 0.0)
    sim._pose_tick()
    assert sent and sent[0]["type"] == "pose"
    a = sim.pose(100.0)["yaw"]
    assert sim.pose(101.0)["yaw"] != a
    sim._handle({"type": "pose_stream", "hz": 0}, 0.0)
    sent.clear()
    sim._pose_next = 0
    sim._pose_tick()
    assert sent == []
