import pytest

from syncvr.library import Library, guess_format
from syncvr.mp4 import read_mp4_info
from syncvr.protocol import is_safe_filename, validate_settings

from conftest import make_mp4


def test_mp4_info_moov_first(tmp_path):
    info = read_mp4_info(make_mp4(tmp_path / "a.mp4", duration=93.5, width=4096, height=2048))
    assert info.duration == pytest.approx(93.5)
    assert (info.width, info.height) == (4096, 2048)
    assert info.faststart is True


def test_mp4_info_moov_last(tmp_path):
    info = read_mp4_info(make_mp4(tmp_path / "b.mp4", duration=12.0, moov_first=False))
    assert info.duration == pytest.approx(12.0)
    assert info.faststart is False


def test_mp4_info_garbage(tmp_path):
    p = tmp_path / "junk.mp4"
    p.write_bytes(b"not a video at all")
    info = read_mp4_info(p)
    assert info.duration is None


@pytest.mark.parametrize("name,projection,stereo", [
    ("concert_360_TB.mp4", "360", "tb"),
    ("Trailer-180-SBS.mp4", "180", "sbs"),
    ("intro_flat.mp4", "flat", "mono"),
    ("dive.mp4", "360", "mono"),
    ("show [180] (LR).mp4", "180", "sbs"),
])
def test_guess_format(name, projection, stereo):
    assert guess_format(name) == {"projection": projection, "stereo": stereo}


def test_library_scan_and_metadata(content_dir):
    lib = Library(content_dir)
    assert lib.scan() is True
    assert set(lib.videos) == {"concert_360_TB.mp4", "trailer_flat.mp4"}
    v = lib.get("concert_360_TB.mp4")
    assert v.duration == pytest.approx(120.0)
    assert (v.projection, v.stereo) == ("360", "tb")
    assert lib.scan() is False  # nothing changed

    lib.update_meta("concert_360_TB.mp4", {"projection": "180", "rotation": 370, "loop": 1})
    assert (v.projection, v.rotation, v.loop) == ("180", 10.0, True)
    with pytest.raises(ValueError):
        lib.update_meta("concert_360_TB.mp4", {"stereo": "weird"})
    with pytest.raises(ValueError):
        lib.update_meta("concert_360_TB.mp4", {"size": 1})

    # Metadata survives a reload.
    lib2 = Library(content_dir, metadata=lib.metadata)
    lib2.scan()
    assert lib2.get("concert_360_TB.mp4").projection == "180"


def test_library_ignores_non_videos(content_dir):
    (content_dir / "notes.txt").write_text("hi")
    (content_dir / "partial.mp4.part").write_bytes(b"x")
    lib = Library(content_dir)
    lib.scan()
    assert "notes.txt" not in lib.videos and "partial.mp4.part" not in lib.videos


@pytest.mark.parametrize("name,ok", [
    ("video.mp4", True), ("My Show (2024).mp4", True), ("../etc/passwd", False),
    ("a/b.mp4", False), (".hidden.mp4", False), ("x.mp4.part", False), ("", False), ("a\\b.mp4", False),
])
def test_safe_filenames(name, ok):
    assert is_safe_filename(name) is ok


def test_validate_settings():
    s = validate_settings({"deadband_ms": 30, "correction_mode": "seek"})
    assert s["deadband_ms"] == 30 and s["correction_mode"] == "seek"
    for bad in ({"nope": 1}, {"deadband_ms": -1}, {"correction_mode": "magic"}, {"rate_gain": "fast"},
                {"hard_seek_ms": True}):
        with pytest.raises(ValueError):
            validate_settings(bad)
