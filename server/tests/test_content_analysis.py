"""ffprobe analysis, the Go limit checks, the moov parser, checksums and the cache."""

import hashlib
import io
import shutil
import subprocess

import pytest

from syncvr import analysis
from syncvr.analysis import ContentAnalyzer, keyframe_stats, probe_file, sha256_file, summarize_probe
from syncvr.controller import Controller, Device
from syncvr.library import Library
from syncvr.limits import ERROR, INFO, WARN, GoLimits, check_video, worst_level
from syncvr.mp4 import moov_before_mdat

from conftest import box, make_mp4

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


def codes(issues, level=None):
    return {i["code"] for i in issues if level is None or i["level"] == level}


def probe(**overrides):
    """A synthetic, perfectly fine probe summary, with fields overridden."""
    video = dict(codec="h264", profile="High", level=41, width=3840, height=1920, fps=30.0, pix_fmt="yuv420p")
    video.update(overrides.pop("video", {}))
    base = dict(error=None, container="mov,mp4,m4a,3gp,3g2,mj2", duration=60.0, bit_rate=20_000_000,
                faststart=True, video=video, audio={"codec": "aac", "channels": 2},
                keyframes={"max": 1.0, "avg": 1.0, "lower_bound": False})
    base.update(overrides)
    return base


# ------------------------------------------------------------ limit checks (no ffmpeg)

def test_good_video_has_no_issues():
    assert check_video(probe()) == []


def test_unknown_probe_means_no_issues():
    assert check_video(None) == []


def test_resolution_limits():
    # H.264 may be 4K UHD or 4096x2048, HEVC only 4096x2048.
    assert check_video(probe(video={"width": 4096, "height": 2048})) == []
    assert check_video(probe(video={"width": 3840, "height": 2160})) == []
    assert "resolution" in codes(check_video(probe(video={"width": 4096, "height": 2160})), WARN)
    assert check_video(probe(video={"codec": "hevc", "profile": "Main", "width": 4096, "height": 2048})) == []
    assert "resolution" in codes(check_video(probe(video={"codec": "hevc", "profile": "Main",
                                                          "width": 3840, "height": 2160})), WARN)


def test_resolution_message():
    issues = check_video(probe(video={"codec": "hevc", "profile": "Main", "width": 5760, "height": 2880}))
    assert issues[0]["level"] == WARN
    assert issues[0]["message"] == "5760×2880 is above the Go's HEVC decoder limit (4096×2048); it may still play"


def test_profiles():
    for ok in ("Baseline", "Constrained Baseline", "Main", "High", "Constrained High"):
        assert "profile" not in codes(check_video(probe(video={"profile": ok})))
    for bad in ("High 10", "High 4:2:2", "High 4:4:4 Predictive"):
        assert "profile" in codes(check_video(probe(video={"profile": bad})), ERROR)
    assert "profile" not in codes(check_video(probe(video={"codec": "hevc", "profile": "Main"})))
    assert "profile" in codes(check_video(probe(video={"codec": "hevc", "profile": "Main 10"})), WARN)
    assert "profile" in codes(check_video(probe(video={"codec": "hevc", "profile": "Main 4:4:4"})), ERROR)


def test_other_codec_warns():
    assert codes(check_video(probe(video={"codec": "av1", "profile": "Main"}))) == {"codec"}


def test_fps_limit():
    assert check_video(probe(video={"fps": 60.0})) == []
    assert check_video(probe(video={"fps": 59.94})) == []
    assert "fps" in codes(check_video(probe(video={"fps": 120.0})), ERROR)


def test_keyframe_interval():
    issues = check_video(probe(keyframes={"max": 10.0, "avg": 9.5, "lower_bound": False}))
    assert [(i["level"], i["message"]) for i in issues] == [(WARN, "Keyframes every 10 s: seeks will be slow")]
    assert check_video(probe(keyframes={"max": 2.0, "avg": 2.0, "lower_bound": False})) == []
    lower = check_video(probe(keyframes={"max": 30.0, "avg": 30.0, "lower_bound": True}))
    assert "at least 30 s" in lower[0]["message"]
    assert check_video(probe(keyframes=None)) == []


def test_moov_at_end_warns_but_unknown_does_not():
    assert codes(check_video(probe(faststart=False)), WARN) == {"moov_at_end"}
    assert check_video(probe(faststart=None)) == []


def test_audio_checks():
    assert codes(check_video(probe(audio=None))) == {"no_audio"}
    assert worst_level(check_video(probe(audio=None))) == INFO
    assert codes(check_video(probe(audio={"codec": "dts"})), WARN) == {"audio_codec"}
    for ok in ("aac", "opus", "mp3"):
        assert check_video(probe(audio={"codec": ok})) == []


def test_probe_failure_is_an_error():
    issues = check_video({"error": "Invalid data found when processing input"})
    assert [i["level"] for i in issues] == [ERROR]
    assert "Invalid data" in issues[0]["message"]
    assert codes(check_video({"error": None, "video": None}), ERROR) == {"no_video"}


def test_errors_sort_before_warnings():
    issues = check_video(probe(faststart=False, audio=None, video={"profile": "Main 4:4:4"}))
    assert [i["level"] for i in issues] == [ERROR, WARN, INFO]


def test_limits_are_configurable():
    strict = GoLimits(max_fps=30.0, max_keyframe_interval_s=0.5)
    found = codes(check_video(probe(video={"fps": 50.0}), strict))
    assert found == {"fps", "keyframes"}


# ------------------------------------------------------------ moov parser (hand-built bytes)

FTYP = box(b"ftyp", b"isom" + bytes(4) + b"isom")


def test_moov_before_mdat_true():
    assert moov_before_mdat(io.BytesIO(FTYP + box(b"moov", bytes(40)) + box(b"mdat", bytes(100)))) is True


def test_moov_after_mdat_false():
    assert moov_before_mdat(io.BytesIO(FTYP + box(b"free", b"") + box(b"mdat", bytes(100)) + box(b"moov", bytes(40)))) is False


def test_mdat_with_64bit_size_is_skipped_correctly():
    import struct
    big = struct.pack(">I4sQ", 1, b"wide", 16 + 10) + bytes(10)
    assert moov_before_mdat(io.BytesIO(FTYP + big + box(b"moov", bytes(8)))) is True


def test_truncated_mdat_still_counts_as_mdat_first():
    data = FTYP + struct_header(b"mdat", 1_000_000) + bytes(50)
    assert moov_before_mdat(io.BytesIO(data)) is False


def test_moov_parser_rejects_junk():
    assert moov_before_mdat(io.BytesIO(b"")) is None
    assert moov_before_mdat(io.BytesIO(b"not an mp4 at all, just text")) is None
    assert moov_before_mdat(io.BytesIO(FTYP)) is None
    assert moov_before_mdat(io.BytesIO(FTYP + bytes(8))) is None  # size 0 / type 0 garbage


def struct_header(kind: bytes, size: int) -> bytes:
    import struct
    return struct.pack(">I4s", size, kind)


# ------------------------------------------------------------ summaries from ffprobe-style JSON

def test_keyframe_stats():
    packets = [{"pts_time": f"{i / 10:.3f}", "flags": "K__" if i % 20 == 0 else "___"} for i in range(100)]
    stats = keyframe_stats(packets, 10.0)
    assert stats["max"] == pytest.approx(2.0) and stats["avg"] == pytest.approx(2.0)
    assert stats["lower_bound"] is False


def test_keyframe_stats_single_keyframe_is_lower_bound():
    packets = [{"pts_time": f"{i / 10:.3f}", "flags": "K__" if i == 0 else "___"} for i in range(100)]
    stats = keyframe_stats(packets, 10.0)
    assert stats["lower_bound"] is True and stats["max"] == pytest.approx(10.0)
    assert keyframe_stats([{"pts_time": "0.0", "flags": "___"}], 10.0) is None
    assert keyframe_stats([], 10.0) is None


def test_summarize_probe():
    info = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5", "bit_rate": "1000000"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "profile": "Main", "level": 123, "width": 4096,
                 "height": 2048, "avg_frame_rate": "30000/1001", "pix_fmt": "yuv420p"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2}]}
    s = summarize_probe(info, None, True)
    assert s["video"]["codec"] == "hevc" and s["video"]["level_text"] == "4.1"
    assert s["video"]["fps"] == pytest.approx(29.97, abs=0.01)
    assert s["audio"]["codec"] == "aac" and s["duration"] == 12.5 and s["faststart"] is True
    assert check_video(s) == []


# ------------------------------------------------------------ real ffmpeg/ffprobe

def _ffmpeg(out, *args, duration=2, audio=False):
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x160:rate=30:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac"]
    cmd += list(args) + [str(out)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return out


H264 = ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast")


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("videos")
    out = {
        "fast": _ffmpeg(d / "fast.mp4", *H264, "-g", "30", "-movflags", "+faststart", audio=True),
        "slow": _ffmpeg(d / "slow.mp4", *H264, "-g", "30"),
        "longgop": _ffmpeg(d / "longgop.mp4", *H264, "-g", "150", "-keyint_min", "150", "-sc_threshold", "0",
                           "-movflags", "+faststart", duration=12),
    }
    encoders = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], stdout=subprocess.PIPE).stdout.decode()
    if "libx265" in encoders:
        out["hevc"] = _ffmpeg(d / "hevc.mp4", "-c:v", "libx265", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                              "-x265-params", "log-level=error:keyint=30", "-tag:v", "hvc1", "-movflags", "+faststart")
    return out


@needs_ffmpeg
def test_probe_faststart_file(videos):
    p = probe_file(FFPROBE, videos["fast"])
    assert p["error"] is None
    assert p["video"]["codec"] == "h264" and (p["video"]["width"], p["video"]["height"]) == (320, 160)
    assert p["video"]["fps"] == pytest.approx(30.0)
    assert p["video"]["profile"] in ("High", "Constrained Baseline", "Main", "Baseline")
    assert p["audio"]["codec"] == "aac"
    assert p["faststart"] is True
    assert p["duration"] == pytest.approx(2.0, abs=0.2)
    assert p["keyframes"]["max"] == pytest.approx(1.0, abs=0.1)
    assert check_video(p) == []


@needs_ffmpeg
def test_probe_moov_at_end(videos):
    p = probe_file(FFPROBE, videos["slow"])
    assert p["faststart"] is False and p["audio"] is None
    assert codes(check_video(p)) == {"moov_at_end", "no_audio"}


@needs_ffmpeg
def test_probe_long_gop(videos):
    p = probe_file(FFPROBE, videos["longgop"])
    assert p["keyframes"]["max"] == pytest.approx(5.0, abs=0.2)
    issues = check_video(p)
    assert "keyframes" in codes(issues, WARN)
    assert "Keyframes every 5 s" in next(i["message"] for i in issues if i["code"] == "keyframes")


@needs_ffmpeg
def test_probe_hevc(videos):
    if "hevc" not in videos:
        pytest.skip("ffmpeg has no libx265 encoder")
    p = probe_file(FFPROBE, videos["hevc"])
    assert p["video"]["codec"] == "hevc" and p["video"]["profile"] == "Main"
    assert not codes(check_video(p), ERROR)


@needs_ffmpeg
def test_probe_garbage_file_is_an_error(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video" * 100)
    p = probe_file(FFPROBE, bad)
    assert p["error"]
    assert check_video(p)[0]["level"] == ERROR


# ------------------------------------------------------------ analyzer: cache, background work, no ffprobe

def test_sha256_file(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 3_000_000)
    assert sha256_file(f) == hashlib.sha256(b"x" * 3_000_000).hexdigest()
    assert sha256_file(f, lambda: True) is None


def fake_probe(calls):
    def run(ffprobe, path, sample_s=30.0):
        calls.append(path.name)
        return probe()
    return run


def test_cache_prevents_reprobing_and_survives_restart(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(analysis, "probe_file", fake_probe(calls))
    f = make_mp4(tmp_path / "a.mp4")
    st = f.stat()
    cache = tmp_path / "data" / "content_cache.json"

    first = ContentAnalyzer(cache, ffprobe="/fake/ffprobe")
    first.request("a.mp4", f, st.st_size, st.st_mtime)
    first.request("a.mp4", f, st.st_size, st.st_mtime)  # already queued or done: no second job
    assert first.wait(10)
    first.request("a.mp4", f, st.st_size, st.st_mtime)
    assert first.wait(10)
    assert calls == ["a.mp4"]
    assert first.sha256_of("a.mp4", st.st_size, st.st_mtime) == hashlib.sha256(f.read_bytes()).hexdigest()
    assert first.analysis_state("a.mp4", st.st_size, st.st_mtime) == "done"
    first.close()

    second = ContentAnalyzer(cache, ffprobe="/fake/ffprobe")  # "restart"
    assert second.analysis_state("a.mp4", st.st_size, st.st_mtime) == "done"
    second.request("a.mp4", f, st.st_size, st.st_mtime)
    assert second.wait(10)
    assert calls == ["a.mp4"]
    assert second.sha256_of("a.mp4", st.st_size, st.st_mtime) == first.sha256_of("a.mp4", st.st_size, st.st_mtime)

    # A changed file is probed and hashed again.
    f.write_bytes(f.read_bytes() + b"more")
    st2 = f.stat()
    assert second.sha256_of("a.mp4", st2.st_size, st2.st_mtime) is None
    second.request("a.mp4", f, st2.st_size, st2.st_mtime)
    assert second.wait(10)
    assert calls == ["a.mp4", "a.mp4"]
    assert second.sha256_of("a.mp4", st2.st_size, st2.st_mtime) == hashlib.sha256(f.read_bytes()).hexdigest()
    second.close()


def test_prune_forgets_removed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "probe_file", fake_probe([]))
    f = make_mp4(tmp_path / "a.mp4")
    st = f.stat()
    a = ContentAnalyzer(tmp_path / "cache.json", ffprobe="/fake/ffprobe")
    a.request("a.mp4", f, st.st_size, st.st_mtime)
    assert a.wait(10)
    a.prune([])
    assert a.sha256_of("a.mp4", st.st_size, st.st_mtime) is None
    assert ContentAnalyzer(tmp_path / "cache.json", ffprobe="/fake/ffprobe").sha256_of(
        "a.mp4", st.st_size, st.st_mtime) is None
    a.close()


def test_without_ffprobe_one_info_note_and_checksums_still_work(tmp_path):
    f = make_mp4(tmp_path / "a.mp4")
    st = f.stat()
    a = ContentAnalyzer(tmp_path / "cache.json", ffprobe=None)
    assert not a.available
    a.request("a.mp4", f, st.st_size, st.st_mtime)
    assert a.wait(10)
    issues = a.issues_of("a.mp4", st.st_size, st.st_mtime)
    assert len(issues) == 1 and issues[0]["level"] == INFO and "ffprobe not found" in issues[0]["message"]
    assert a.analysis_state("a.mp4", st.st_size, st.st_mtime) == "unavailable"
    assert a.sha256_of("a.mp4", st.st_size, st.st_mtime) == hashlib.sha256(f.read_bytes()).hexdigest()
    a.close()


def test_probe_runs_off_the_calling_thread(tmp_path, monkeypatch):
    import threading
    seen = []
    monkeypatch.setattr(analysis, "probe_file", lambda *a, **k: seen.append(threading.current_thread()) or probe())
    f = make_mp4(tmp_path / "a.mp4")
    st = f.stat()
    a = ContentAnalyzer(None, ffprobe="/fake/ffprobe")
    a.request("a.mp4", f, st.st_size, st.st_mtime)
    assert a.wait(10)
    assert seen and seen[0] is not threading.current_thread()
    a.close()


def test_on_update_is_called(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "probe_file", fake_probe([]))
    f = make_mp4(tmp_path / "a.mp4")
    st = f.stat()
    a = ContentAnalyzer(None, ffprobe="/fake/ffprobe")
    hits = []
    a.on_update = lambda: hits.append(1)
    a.request("a.mp4", f, st.st_size, st.st_mtime)
    assert a.wait(10)
    assert len(hits) == 2  # probe + checksum
    a.close()


# ------------------------------------------------------------ library JSON and sync_content

def test_library_json_carries_analysis(content_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "probe_file", fake_probe([]))
    a = ContentAnalyzer(tmp_path / "cache.json", ffprobe="/fake/ffprobe")
    lib = Library(content_dir, analyzer=a)
    lib.scan()
    assert a.wait(10)
    for entry in lib.to_json():
        assert entry["analysis"] == "done" and entry["issues"] == []
        assert entry["sha256"] == hashlib.sha256((content_dir / entry["name"]).read_bytes()).hexdigest()
        assert entry["probe"]["video"]["codec"] == "h264"
    a.close()


class FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def content_url(self, name):
        return f"http://server/content/{name}"


def test_sync_content_includes_sha256_when_known(content_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "probe_file", fake_probe([]))
    a = ContentAnalyzer(tmp_path / "cache.json", ffprobe="/fake/ffprobe")
    lib = Library(content_dir, analyzer=a)
    lib.scan()
    ctl = Controller(lib)
    conn = FakeConn()
    dev = ctl.devices["h1"] = Device(device_id="h1", online=True, conn=conn)

    # Right after the scan the checksum may still be running: a name without a hash is allowed.
    ctl.distributor.request("h1", ["trailer_flat.mp4"], False)
    first = conn.sent[-1]["files"][0]
    assert set(first) - {"sha256"} == {"name", "size", "url"}

    assert a.wait(10)
    ctl.distributor.finished("h1", {})
    dev.inventory.clear()
    ctl.distributor.request("h1", ["trailer_flat.mp4", "concert_360_TB.mp4"], False)
    files = {f["name"]: f for f in conn.sent[-1]["files"]}
    for name, f in files.items():
        assert f["sha256"] == hashlib.sha256((content_dir / name).read_bytes()).hexdigest()
        assert f["size"] == (content_dir / name).stat().st_size
    a.close()


def test_sync_content_omits_sha256_without_analyzer(content_dir):
    lib = Library(content_dir)
    lib.scan()
    ctl = Controller(lib)
    conn = FakeConn()
    ctl.devices["h1"] = Device(device_id="h1", online=True, conn=conn)
    ctl.distributor.request("h1", ["trailer_flat.mp4"], False)
    assert "sha256" not in conn.sent[-1]["files"][0]
