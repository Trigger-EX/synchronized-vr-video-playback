from syncvr.gui import format as fmt


def test_fmt_time_and_bytes():
    assert fmt.fmt_time(None) == "–"
    assert fmt.fmt_time(float("inf")) == "–"
    assert fmt.fmt_time(-3) == "0:00"
    assert fmt.fmt_time(65.9) == "1:05"
    assert fmt.fmt_time(3725) == "1:02:05"
    assert fmt.fmt_bytes(None) == "–"
    assert fmt.fmt_bytes(512) == "512 B"
    assert fmt.fmt_bytes(1536) == "1.5 KB"
    assert fmt.fmt_bytes(3 * 1024 ** 3) == "3.0 GB"


def test_drift_class_thresholds():
    assert fmt.drift_class(24.9) == "good"
    assert fmt.drift_class(-25) == "meh"
    assert fmt.drift_class(79) == "meh"
    assert fmt.drift_class(-80) == "poor"


def test_position_of():
    assert fmt.position_of(None, 5) is None
    assert fmt.position_of({"mode": "stopped"}, 5) is None
    assert fmt.position_of({"mode": "paused", "pos": 12.0}, 99) == 12.0
    playing = {"mode": "playing", "pos": 10.0, "at": 100.0, "duration": 60.0}
    assert fmt.position_of(playing, 95) == 10.0  # not started yet
    assert fmt.position_of(playing, 105) == 15.0
    assert fmt.position_of(playing, 500) == 60.0
    assert fmt.position_of(dict(playing, loop=True), 160) == 10.0


def test_state_name_and_classes():
    assert fmt.state_name({"online": False, "status": {"state": "playing"}}) == "offline"
    assert fmt.state_name({"online": True, "status": {}}) == "connecting"
    assert fmt.state_name({"online": True, "status": {"state": "playing"}}) == "playing"
    assert (fmt.battery_class(19), fmt.battery_class(39), fmt.battery_class(40)) == ("poor", "meh", "")
    assert (fmt.wifi_class(49), fmt.wifi_class(50), fmt.wifi_class(69), fmt.wifi_class(70)) == ("poor", "meh", "meh", "")
    assert (fmt.wifi_percent(-120), fmt.wifi_percent(-80), fmt.wifi_percent(-60), fmt.wifi_percent(-30)) == (0, 40, 80, 100)
    assert fmt.fmt_current(1.234) == "+1.23 A" and fmt.fmt_current(-0.5) == "-0.50 A"
    assert fmt.current_class(0.2, True, 50) == "meh" and fmt.current_class(0.2, True, 100) == ""
    assert fmt.current_class(0.5, True, 50) == "" and fmt.current_class(-0.2, False, 50) == ""
    assert fmt.rtt_class(51) == "meh" and fmt.temp_class(43) == "poor"


def test_device_info_and_content_summary():
    lib = [{"name": "a.mp4", "size": 10}, {"name": "b.mp4", "size": 20}]
    dev = {"inventory": {"a.mp4": 10, "b.mp4": 5},
           "status": {"battery": 0.15, "charging": True, "temp_c": 44.0, "worn": False, "rtt_ms": 12.0,
                      "wifi_rssi": -80, "battery_current_a": 0.2, "storage_free": 1024 ** 3}}
    assert fmt.content_summary(dev, lib) == "1/2 videos"
    assert fmt.content_summary(dev, []) == ""
    info = dict(fmt.device_info(dev, lib))
    assert info["battery 15% (charging)"] == "poor"
    assert info["44°C"] == "poor"
    assert "not worn" in info and info["wifi 40%"] == "poor"
    assert info["+0.20 A"] == "meh"
    assert info["1.0 GB free"] == "meh"
    assert info["1/2 videos"] == ""
    assert fmt.device_info({"status": {"battery": -1}}, []) == []


def test_fleet_stats():
    devs = [{"online": True, "status": {"state": "playing", "drift_ms": -30}},
            {"online": True, "status": {"state": "playing", "drift_ms": 10}},
            {"online": True, "status": {"state": "paused"}},
            {"online": False, "status": {"state": "playing", "drift_ms": 999}}]
    assert fmt.fleet_stats(devs) == {"total": 4, "online": 3, "playing": 2, "worst_drift": 30}
    assert fmt.fleet_stats([])["worst_drift"] is None


def test_sort_devices_and_groups():
    devs = [{"label": "Go 10", "online": True, "group": "b"}, {"label": "Go 2", "online": True, "group": "a"},
            {"label": "Go 1", "online": False, "group": ""}]
    assert [d["label"] for d in fmt.sort_devices(devs)] == ["Go 2", "Go 10", "Go 1"]
    assert fmt.groups_of(devs) == ["a", "b"]


def test_probe_and_issues_summary():
    assert fmt.probe_summary({"width": 1920, "height": 1080}) == "1920×1080"
    assert fmt.probe_summary({}) == ""
    v = {"probe": {"video": {"width": 3840, "height": 1920, "codec": "h264", "profile": "High",
                             "level_text": "5.1", "fps": 29.97}, "bit_rate": 20e6,
                   "keyframes": {"max": 2.0, "lower_bound": False}}}
    assert fmt.probe_summary(v) == "3840×1920 · h264 High L5.1 · 29.97 fps · 20.0 Mb/s · GOP 2 s"
    assert fmt.issues_summary({}) == ("", "")
    assert fmt.issues_summary({"analysis": "pending"}) == ("checking…", "pending")
    assert fmt.issues_summary({"analysis": "done", "issues": []}) == ("OK", "ok")
    issues = [{"level": "info"}, {"level": "warn"}, {"level": "warn"}]
    assert fmt.issues_summary({"analysis": "done", "issues": issues}) == ("2 warnings", "warn")
    assert fmt.issues_summary({"analysis": "done", "issues": [{"level": "error"}]}) == ("1 error", "error")
