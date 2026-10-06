from syncvr.gui.proxy import (ProxyJob, cache_path, ffmpeg_args, parse_progress, proxy_plan,
                              stale_proxies)


def test_360_mono_5120_downscales():
    assert proxy_plan(5120, 2560, "360", "mono") == (3456, 1728, "")


def test_360_mono_small_uses_original():
    assert proxy_plan(3456, 1728, "360", "mono") is None
    assert proxy_plan(3200, 1600, "360", "mono") is None


def test_360_sbs_and_tb():
    w, h, c = proxy_plan(10240, 2560, "360", "sbs")  # eye 5120x2560
    assert (w, h, c) == (3456, 1728, "crop=5120:2560:0:0")
    w, h, c = proxy_plan(5120, 5120, "360", "tb")
    assert (w, h, c) == (3456, 1728, "crop=5120:2560:0:0")


def test_stereo_never_upscales_but_still_crops():
    assert proxy_plan(4096, 2048, "360", "sbs") == (2048, 2048, "crop=2048:2048:0:0")


def test_180():
    assert proxy_plan(5760, 2880, "180", "sbs") == (1728, 1728, "crop=2880:2880:0:0")
    assert proxy_plan(4096, 4096, "180", "mono") == (1728, 1728, "")
    assert proxy_plan(2048, 2048, "180", "mono") is None


def test_flat_cap_1920_keeps_aspect():
    assert proxy_plan(3840, 2160, "flat", "mono") == (1920, 1080, "")
    assert proxy_plan(1280, 720, "flat", "mono") is None
    assert proxy_plan(3840, 1080, "flat", "sbs") == (1920, 1080, "crop=1920:1080:0:0")


def test_even_dims_and_unknown():
    for args in [(5121, 2561, "360", "mono"), (3841, 2161, "flat", "mono"), (7001, 3501, "180", "sbs")]:
        w, h, _ = proxy_plan(*args)
        assert w % 2 == 0 and h % 2 == 0
    assert proxy_plan(None, 100, "360", "mono") is None


def test_ffmpeg_args():
    a = ffmpeg_args("in.mp4", "out.mp4", (4608, 2304, ""), 29.97, "aac")
    assert a[a.index("-vf") + 1] == "scale=4608:2304"
    assert a[a.index("-g") + 1] == "30" and a[a.index("-crf") + 1] == "21"
    assert a[a.index("-c:a") + 1] == "copy" and a[-1] == "out.mp4"
    b = ffmpeg_args("in.mp4", "out.mp4", (10, 10, "crop=20:10:0:0"), None, "mp3")
    assert b[b.index("-vf") + 1] == "crop=20:10:0:0,scale=10:10"
    assert b[b.index("-c:a") + 1] == "aac" and "128k" in b


def test_cache_path_and_stale(tmp_path):
    p = cache_path(tmp_path, "my vid.mp4", 100, 5.7, (4608, 2304, ""))
    assert p.parent.name == ".laptop" and p.name == "my_vid-100-5-crf21-4608x2304.mp4"
    p.parent.mkdir()
    p.write_bytes(b"x")
    assert stale_proxies(tmp_path, "my vid.mp4") == [p]


def test_parse_progress():
    assert parse_progress("out_time_us=5000000", 10) == 50.0
    assert parse_progress("frame=3", 10) is None


class FakeProc:
    def __init__(self, lines, rc, part):
        self.stdout, self.rc, self.part = lines, rc, part

    def wait(self):
        if self.rc == 0:
            open(self.part, "wb").write(b"x")
        return self.rc


def run_job(tmp_path, rc):
    dst = tmp_path / ".laptop" / "o.mp4"
    prog, done = [], []
    def popen(args, **kw):
        return FakeProc(["out_time_us=1000000\n"], rc, args[-1])
    j = ProxyJob(["ffmpeg", str(dst)], dst, 2.0, prog.append, lambda o, e: done.append((o, e)), popen)
    j._run()
    return dst, prog, done


def test_job_success_renames(tmp_path):
    dst, prog, done = run_job(tmp_path, 0)
    assert prog == [50.0] and done == [(str(dst), None)]
    assert dst.is_file() and not dst.with_name("o.mp4.part").exists()


def test_job_failure(tmp_path):
    dst, _p, done = run_job(tmp_path, 1)
    assert done[0][0] is None and "code 1" in done[0][1] and not dst.exists()
