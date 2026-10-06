"""Crash recovery: a restarted controller adopts what the headsets report instead of re-cueing them."""
import logging
import time

import pytest

import syncvr.controller as ctl
from syncvr.controller import Controller, position_at
from syncvr.library import Library

V1, V2 = "concert_360_TB.mp4", "trailer_flat.mp4"


class FakeConn:
    remote_ip = "10.0.0.2"
    http_port = 8080

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass

    def content_url(self, name):
        return f"http://10.0.0.2/content/{name}"

    def kinds(self):
        return [m["type"] for m in self.sent]


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(ctl, "server_clock", lambda: t[0])
    return t


def playing_desired(video, pos, at):
    return {"video": video, "projection": "equirect360", "stereo": "tb", "rotation": 0.0, "duration": 120.0,
            "mode": "playing", "pos": pos, "at": at, "loop": False}


def state_with(desired: dict, saved_wall=None):
    return {"saved_wall": time.time() if saved_wall is None else saved_wall,
            "devices": {i: {"name": i, "desired": d} for i, d in desired.items()}}


def restart(content_dir, state):
    lib = Library(content_dir)
    lib.scan()
    c = Controller(lib)
    c.load(state)
    return c


def connect(c, device_id):
    conn = FakeConn()
    c.headset_connected({"device_id": device_id}, conn)
    return conn


def status(c, device_id, state="playing", video=V1, position=None, expected=None, anchor=None, synced=True):
    msg = {"type": "status", "state": state, "video": video, "position": position, "expected": expected,
           "clock_synced": synced}
    if anchor:
        msg["anchor"] = anchor
    c.headset_message(c.devices[device_id], msg)


def plays(conn):
    return [m for m in conn.sent if m["type"] in ("play", "pause", "stop")]


def test_dump_load_roundtrip_keeps_desired(content_dir):
    c = restart(content_dir, state_with({"a": playing_desired(V1, 5.0, 900.0)}))
    assert c.any_active()
    state = c.dump()
    assert abs(state["saved_wall"] - time.time()) < 5
    assert state["devices"]["a"]["desired"]["pos"] == 5.0
    assert not restart(content_dir, {"devices": {"a": {}}}).any_active()


def test_held_back_until_status_then_anchor_copied_exactly(content_dir, clock):
    c = restart(content_dir, state_with({"a": playing_desired(V1, 0.0, 800.0)}))
    assert c.recovering_until == 1020.0
    conn = connect(c, "a")
    assert plays(conn) == []  # held back while recovering
    anchor = {"pos": 40.0, "at": 960.0, "loop": False}
    status(c, "a", position=80.0, expected=80.0, anchor=anchor)  # 40 + (1000 - 960)
    assert c.devices["a"].desired["pos"] == 40.0 and c.devices["a"].desired["at"] == 960.0
    assert plays(conn) == []  # already on that anchor: nothing to send
    snap = c.snapshot()["server"]["recovery"]
    assert snap["active"] and snap["rejoined"] == 1 and snap["video"] == V1


def test_rebuild_without_anchor_uses_expected_and_receive_time(content_dir, clock):
    c = restart(content_dir, state_with({"a": playing_desired(V1, 0.0, 800.0)}))
    conn = connect(c, "a")
    status(c, "a", position=61.0, expected=62.0)
    d = c.devices["a"].desired
    assert d["mode"] == "playing" and d["pos"] == 62.0 and d["at"] == 1000.0
    assert plays(conn) == []


def test_anchor_failing_sanity_check_is_rebuilt(content_dir, clock, caplog):
    c = restart(content_dir, state_with({}) | {"devices": {"a": {}}})
    connect(c, "a")
    with caplog.at_level(logging.WARNING):
        status(c, "a", position=30.0, expected=30.0, anchor={"pos": 90.0, "at": 1000.0, "loop": False})
    assert c.devices["a"].desired["pos"] == 30.0
    assert "rebuilding" in caplog.text


def test_group_within_quarter_second_snaps_to_median(content_dir, clock):
    c = restart(content_dir, state_with({i: None for i in "abcd"}))
    conns = {i: connect(c, i) for i in "abcd"}
    # implied positions at t=1000: 50.00, 50.10, 50.20, 50.30 (all within 0.25 of the one arriving)
    for i, p in zip("abcd", (50.0, 50.1, 50.2, 50.3)):
        status(c, i, position=p, expected=p, anchor={"pos": p, "at": 1000.0, "loop": False})
    desired = {i: c.devices[i].desired for i in "abcd"}
    assert len({(d["pos"], d["at"]) for d in desired.values()}) == 1
    assert desired["a"]["pos"] == pytest.approx(50.1)  # lower median of four
    for i in "acd":  # everyone but the median headset ends up cued to its anchor
        assert plays(conns[i])[-1]["type"] == "play" and plays(conns[i])[-1]["pos"] == pytest.approx(50.1)


def test_far_apart_headsets_are_not_grouped(content_dir, clock):
    c = restart(content_dir, state_with({"a": None, "b": None}))
    connect(c, "a"), connect(c, "b")
    status(c, "a", position=50.0, expected=50.0, anchor={"pos": 50.0, "at": 1000.0, "loop": False})
    status(c, "b", position=51.0, expected=51.0, anchor={"pos": 51.0, "at": 1000.0, "loop": False})
    assert c.devices["a"].desired["pos"] == 50.0 and c.devices["b"].desired["pos"] == 51.0


def test_paused_state_is_kept(content_dir, clock):
    c = restart(content_dir, state_with({"a": playing_desired(V1, 0.0, 800.0)}))
    conn = connect(c, "a")
    status(c, "a", state="paused", position=33.0)
    d = c.devices["a"].desired
    assert d["mode"] == "paused" and d["pos"] == 33.0
    assert plays(conn) == []


def test_two_groups_two_videos(content_dir, clock):
    c = restart(content_dir, state_with({i: None for i in ("a", "b", "c", "d")}))
    for i in "abcd":
        connect(c, i)
    for i, v in zip("abcd", (V1, V1, V2, V2)):
        status(c, i, video=v, position=10.0, expected=10.0, anchor={"pos": 10.0, "at": 1000.0, "loop": False})
    assert {c.devices[i].desired["video"] for i in "ab"} == {V1}
    assert {c.devices[i].desired["video"] for i in "cd"} == {V2}
    clock[0] = 1021.0
    c.recovery_tick()
    assert not c.snapshot()["server"]["recovery"]["active"]
    assert any("Recovered show: 4 headsets" in e["message"] for e in c.events)


def test_stale_file_ignored(content_dir, clock):
    old = time.time() - 31 * 60
    c = restart(content_dir, state_with({"a": playing_desired(V1, 0.0, 800.0)}, saved_wall=old))
    assert c.devices["a"].desired is None and not c.any_active()
    conn = connect(c, "a")
    status(c, "a", state="idle", video=None)
    assert plays(conn) == []


def test_status_timeout_reconciles_without_clock_sync(content_dir, clock):
    c = restart(content_dir, state_with({"a": playing_desired(V1, 0.0, 800.0)}))
    conn = connect(c, "a")
    status(c, "a", position=5.0, expected=5.0, synced=False)
    assert plays(conn) == [] and "a" not in c._members
    clock[0] = 1003.5
    c.recovery_tick()
    assert c.devices["a"].desired["pos"] == 5.0


def test_late_headset_joins_consensus(content_dir, clock):
    saved = playing_desired(V1, 0.0, 800.0)
    c = restart(content_dir, state_with({"a": None, "b": None, "late": saved, "offline": saved}))
    for i in "ab":
        connect(c, i)
        status(c, i, position=70.0, expected=70.0, anchor={"pos": 20.0, "at": 950.0, "loop": False})
    late = connect(c, "late")
    status(c, "late", state="idle", video=None)
    sent = plays(late)
    assert sent[-1]["type"] == "play" and (sent[-1]["pos"], sent[-1]["at"]) == (20.0, 950.0)
    clock[0] = 1020.5
    c.recovery_tick()
    off = c.devices["offline"].desired
    assert (off["pos"], off["at"]) == (20.0, 950.0)  # given the consensus at the end of the window
    msgs = [e["message"] for e in c.events if "Recovered show" in e["message"]]
    assert msgs and "3 headsets rejoined at 01:30 (concert_360_TB.mp4)" in msgs[0]


def test_operator_command_cancels_recovery_for_its_targets(content_dir, clock):
    c = restart(content_dir, state_with({"a": playing_desired(V1, 0.0, 800.0), "b": playing_desired(V1, 0.0, 800.0)}))
    ca, cb = connect(c, "a"), connect(c, "b")
    c.dirty = False
    c.execute("pause", {"targets": ["a"]})
    assert c.dirty and "a" in c._cancelled
    status(c, "a", position=70.0, expected=70.0, anchor={"pos": 70.0, "at": 1000.0, "loop": False})
    assert c.devices["a"].desired["mode"] == "paused"  # the late status does not undo the command
    assert "a" not in c._members
    status(c, "b", position=70.0, expected=70.0, anchor={"pos": 70.0, "at": 1000.0, "loop": False})
    assert "b" in c._members
