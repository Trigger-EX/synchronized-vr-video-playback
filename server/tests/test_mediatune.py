from syncvr.gui.mediatune import FrameGate, FrameStats, configure_hwdec, hwdec_default


def test_gate_admits_at_target_rate():
    g = FrameGate(20)
    assert g.wait(0.0) == 0.0
    assert abs(g.wait(0.01) - 0.04) < 1e-9  # too early: wait for the slot
    assert g.wait(0.05) == 0.0
    admitted = sum(1 for i in range(120) if g.wait(1.0 + i / 60.0) == 0.0)  # 2 s of 60 fps
    assert 38 <= admitted <= 42
    g.reset()
    assert g.wait(0.0) == 0.0


def test_hwdec_defaults_per_platform_and_user_override():
    assert hwdec_default("linux") == "vaapi,cuda"
    assert hwdec_default("win32") == "d3d11va,cuda"
    assert hwdec_default("darwin") == "videotoolbox"
    assert hwdec_default("freebsd") == ""
    env = {}
    assert configure_hwdec(env, "linux") == "vaapi,cuda" and env["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"]
    env = {"QT_FFMPEG_DECODING_HW_DEVICE_TYPES": "cuda"}
    assert configure_hwdec(env, "linux") == "cuda"
    env = {"SYNCVR_LOCAL_HWDEC": "0"}
    assert configure_hwdec(env, "linux") == "none"
    env = {"SYNCVR_LOCAL_HWDEC": "0", "QT_FFMPEG_DECODING_HW_DEVICE_TYPES": "vaapi"}
    assert configure_hwdec(env, "linux") == "vaapi"
    assert configure_hwdec({}, "freebsd") == ""


def test_stats_report_every_interval():
    s = FrameStats(10.0)
    out = None
    for i in range(700):  # 70 s at 10 fps received, every other one converted at 8 ms
        out = s.add(i / 10.0, i % 2 == 0, 0.008) or out
        if i == 50:
            assert out is None
    assert out and "/s converted" in out and "8.0 ms" in out
