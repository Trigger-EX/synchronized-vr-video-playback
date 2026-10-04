from pathlib import Path

from syncvr import launcher, paths


def test_user_data_dir_per_platform(tmp_path):
    home = tmp_path / "home"
    assert paths.user_data_dir("linux", {"XDG_DATA_HOME": "/x/share"}, home) == Path("/x/share/SyncVR")
    assert paths.user_data_dir("linux", {}, home) == home / ".local" / "share" / "SyncVR"
    assert paths.user_data_dir("linux", {"XDG_DATA_HOME": "relative"}, home) == home / ".local" / "share" / "SyncVR"
    assert paths.user_data_dir("win32", {"LOCALAPPDATA": "C:/L"}, home) == Path("C:/L") / "SyncVR"
    assert paths.user_data_dir("win32", {}, home) == home / "AppData" / "Local" / "SyncVR"
    assert paths.user_data_dir("darwin", {}, home) == home / "Library" / "Application Support" / "SyncVR"


def test_source_layout_unchanged():
    assert paths.data_dir(frozen=False) == paths.SOURCE_DIR / "data"
    assert paths.content_dir(paths.SOURCE_DIR / "data", frozen=False) == paths.SOURCE_DIR / "content"


def test_frozen_uses_user_dir(tmp_path):
    exe = tmp_path / "app"
    exe.mkdir()
    data = paths.data_dir(frozen=True, exe_dir=exe, platform="linux", env={"XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert data == tmp_path / "xdg" / "SyncVR"
    assert paths.content_dir(data, frozen=True, exe_dir=exe) == data / "content"


def test_portable_marker_uses_folders_beside_exe(tmp_path):
    exe = tmp_path / "app"
    exe.mkdir()
    (exe / "portable").write_text("")
    assert paths.data_dir(frozen=True, exe_dir=exe, platform="linux", env={}) == exe / "data"
    assert paths.content_dir(exe / "data", frozen=True, exe_dir=exe) == exe / "content"


class Args:
    content = None
    data = None
    http_port = None
    no_discovery = True
    self_test = False


def test_make_config_uses_launcher_json_content(tmp_path):
    args = Args()
    args.data = str(tmp_path / "data")
    (tmp_path / "data").mkdir()
    cfg = launcher._make_config(args)
    assert cfg.content_dir == paths.SOURCE_DIR / "content" or cfg.content_dir.name == "content"
    chosen = tmp_path / "videos"
    launcher.save_override(tmp_path / "data", "content", str(chosen))
    assert launcher.load_overrides(tmp_path / "data")["content"] == str(chosen)
    assert launcher._make_config(args).content_dir == chosen
    launcher.save_override(tmp_path / "data", "name", "Lobby")
    assert launcher.load_overrides(tmp_path / "data") == {"content": str(chosen), "name": "Lobby"}


def test_instance_lock(qapp, tmp_path):
    from syncvr.gui.app import acquire_instance_lock
    first = acquire_instance_lock(tmp_path)
    assert first is not None
    assert acquire_instance_lock(tmp_path) is None
    first.unlock()
    again = acquire_instance_lock(tmp_path)
    assert again is not None
    again.unlock()


def test_self_test_flag(qapp, tmp_path, monkeypatch):
    from syncvr.__main__ import build_parser
    monkeypatch.setattr(launcher, "port_in_use", lambda *a, **k: False)
    args = build_parser().parse_args(["gui", "--self-test"])
    assert launcher.main(args) == 0
