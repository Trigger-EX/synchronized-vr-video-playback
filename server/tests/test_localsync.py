from syncvr.gui.localsync import LocalSync, plan


def snap(desired, dev_id="a"):
    return {"devices": [{"id": dev_id, "desired": desired}]}


def playing(video="v.mp4", pos=10.0, at=100.0):
    return {"mode": "playing", "video": video, "pos": pos, "at": at, "duration": 600}


def test_stop_cases(tmp_path):
    assert plan(snap(None), "a", 0, tmp_path, None)["mode"] == "stop"
    assert plan(snap(playing()), "zz", 0, tmp_path, None)["mode"] == "stop"
    assert plan(None, "a", 0, tmp_path, None)["mode"] == "stop"


def test_play_and_pause(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"x")
    p = plan(snap(playing()), "a", 105.0, tmp_path, None)
    assert p["mode"] == "play" and p["target_pos"] == 15.0 and p["seek"] and p["path"] == str(tmp_path / "v.mp4")
    d = dict(playing(), mode="paused")
    p = plan(snap(d), "a", 105.0, tmp_path, 10.0)
    assert p["mode"] == "pause" and p["target_pos"] == 10.0 and not p["seek"]


def test_errors(tmp_path):
    assert "not found" in plan(snap(playing()), "a", 100, tmp_path, 0)["error"]
    for bad in ("../x.mp4", ".hidden", "a/b.mp4"):
        p = plan(snap(playing(video=bad)), "a", 100, tmp_path, 0)
        assert "error" in p and "path" not in p and not p["seek"]


def test_threshold_and_cooldown(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"x")
    s = snap(playing())
    ls = LocalSync()
    assert not ls.step(s, "a", 100.0, tmp_path, 10.1)["seek"]
    assert ls.step(s, "a", 101.0, tmp_path, 5.0)["seek"]
    assert not ls.step(s, "a", 102.0, tmp_path, 5.0)["seek"]  # cooldown
    assert ls.step(s, "a", 102.6, tmp_path, 5.0)["seek"]
    ls.step(snap(None), "a", 103.0, tmp_path, 5.0)  # stop resets
    assert ls.step(s, "a", 103.1, tmp_path, 0.0)["seek"]
