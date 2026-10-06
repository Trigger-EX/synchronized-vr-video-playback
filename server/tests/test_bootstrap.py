import sys
from pathlib import Path

from syncvr import bootstrap


def test_venv_python_per_platform(monkeypatch):
    v = Path("v")
    monkeypatch.setattr(sys, "platform", "linux")
    assert bootstrap.venv_python(v) == v / "bin" / "python"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert bootstrap.venv_python(v) == v / "bin" / "python"
    monkeypatch.setattr(sys, "platform", "win32")
    assert bootstrap.venv_python(v, gui_exe=False) == v / "Scripts" / "python.exe"
    assert bootstrap.venv_python(v, gui_exe=True) == v / "Scripts" / "pythonw.exe"
    monkeypatch.setattr(sys, "executable", "/x/pythonw.exe")
    assert bootstrap.venv_python(v).name == "pythonw.exe"


def test_needs_install_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert bootstrap.needs_install(tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("")
    assert bootstrap.needs_install(tmp_path)  # no stamp
    (tmp_path / bootstrap.STAMP_NAME).write_text(bootstrap._stamp_text())
    assert not bootstrap.needs_install(tmp_path)
    (tmp_path / bootstrap.STAMP_NAME).write_text("old\n")
    assert bootstrap.needs_install(tmp_path)


def test_no_stamp_when_pip_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("")
    monkeypatch.setattr(bootstrap, "_pip_install", lambda py, reqs=None: False)
    assert bootstrap.ensure_venv(tmp_path) is None
    assert not (tmp_path / bootstrap.STAMP_NAME).exists()
    monkeypatch.setattr(bootstrap, "_pip_install", lambda py, reqs=None: True)
    assert bootstrap.ensure_venv(tmp_path) == tmp_path / "bin" / "python"
    assert (tmp_path / bootstrap.STAMP_NAME).exists()


def test_requirements_match_pyproject():
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        import pytest
        pytest.skip("tomllib needs Python 3.11")
    data = tomllib.loads((bootstrap.SERVER_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    opt = data["project"]["optional-dependencies"]
    assert set(bootstrap.REQUIREMENTS) == set(data["project"]["dependencies"]) | set(opt["gui"])
    assert set(bootstrap.MEDIA_REQUIREMENTS) == set(opt["media"])


def _fake_venv(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("")
    (tmp_path / bootstrap.STAMP_NAME).write_text("\n".join(bootstrap.REQUIREMENTS) + "\n")


def test_install_media_stamps_and_keeps_requirement_set(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _fake_venv(tmp_path)
    assert not bootstrap.needs_install(tmp_path)
    calls = []
    assert bootstrap.install_media(tmp_path, installer=lambda py, reqs: calls.append((py, reqs)) or True)
    assert calls == [(tmp_path / "bin" / "python", bootstrap.REQUIREMENTS + bootstrap.MEDIA_REQUIREMENTS)]
    assert bootstrap.media_installed(tmp_path)
    assert not bootstrap.needs_install(tmp_path)  # stamp updated, media stays in the set
    (tmp_path / bootstrap.STAMP_NAME).write_text("\n".join(bootstrap.REQUIREMENTS) + "\n")
    assert bootstrap.needs_install(tmp_path)  # core-only stamp no longer matches once media is installed


def test_install_media_failure_leaves_no_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _fake_venv(tmp_path)
    assert not bootstrap.install_media(tmp_path, installer=lambda py, reqs: False)
    assert not bootstrap.media_installed(tmp_path)
    assert not bootstrap.needs_install(tmp_path)
    assert not bootstrap.install_media(tmp_path / "missing", installer=lambda py, reqs: True)


def test_running_from_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert bootstrap.running_from_venv(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not bootstrap.running_from_venv(tmp_path)
