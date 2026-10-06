import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QComboBox, QSpinBox  # noqa: E402

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui import library as lib  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402


def video(name="a.mp4", **over):
    v = {"name": name, "title": "Alpha", "size": 10, "duration": 125.0, "projection": "360", "stereo": "mono",
         "rotation": 0, "loop": False, "analysis": "done", "issues": [], "width": 3840, "height": 1920}
    v.update(over)
    return v


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    yield w
    w.close()


def feed(win, library, devices=()):
    win.bridge.state.emit(make_snapshot(library=library, devices=list(devices)))
    return win.library


def cell(tab, row, col, role=Qt.DisplayRole):
    return tab.model.index(row, col).data(role)


def test_rows_and_counts(win):
    tab = feed(win, [video(issues=[{"level": "warn", "message": "long GOP"}], sha256="ab" * 32),
                     video("b.mp4", title="", size=20, analysis="pending")],
               [make_device("x", inventory={"a.mp4": 10, "b.mp4": 5}), make_device("y", inventory={"a.mp4": 10})])
    m = tab.model
    assert m.rowCount() == 2 and tab.empty_label.isHidden()
    assert [cell(tab, 0, c) for c in (lib.TITLE, lib.CHECKS, lib.LENGTH, lib.SIZE, lib.PROJECTION, lib.STEREO,
                                      lib.ROTATION, lib.ON_HEADSETS)] == \
        ["Alpha", "1 warning", "2:05", "10 B", "360°", "Mono", 0, "2 / 2"]
    assert cell(tab, 0, lib.FILE).startswith("a.mp4\n3840×1920")
    assert "warn: long GOP" in cell(tab, 0, lib.CHECKS, Qt.ToolTipRole)
    assert cell(tab, 1, lib.CHECKS) == "checking…" and cell(tab, 1, lib.ON_HEADSETS) == "0 / 2"
    assert cell(tab, 0, lib.LOOP, Qt.CheckStateRole) == Qt.Unchecked
    flags = m.flags(m.index(0, lib.PROJECTION))
    assert flags & Qt.ItemIsEditable and not m.flags(m.index(0, lib.SIZE)) & Qt.ItemIsEditable


def test_edits_go_through_bridge(win):
    tab = feed(win, [video()])
    m = tab.model
    assert m.setData(m.index(0, lib.PROJECTION), "flat")
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"projection": "flat"})
    assert m.setData(m.index(0, lib.ROTATION), 90)
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"rotation": 90})
    assert m.setData(m.index(0, lib.LOOP), Qt.Checked, Qt.CheckStateRole)
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"loop": True})
    assert m.setData(m.index(0, lib.TITLE), "New")
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"title": "New"})
    n = len(win.bridge.calls)
    assert not m.setData(m.index(0, lib.TITLE), "New") and not m.setData(m.index(0, lib.SIZE), 5)
    assert len(win.bridge.calls) == n


def test_delegates_create_editors(win):
    tab = feed(win, [video(stereo="tb", rotation=45)])
    idx = tab.model.index(0, lib.STEREO)
    combo = tab.table.itemDelegateForColumn(lib.STEREO).createEditor(tab, None, idx)
    assert isinstance(combo, QComboBox)
    tab.table.itemDelegateForColumn(lib.STEREO).setEditorData(combo, idx)
    assert combo.currentData() == "tb"
    combo.setCurrentIndex(combo.findData("sbs"))
    tab.table.itemDelegateForColumn(lib.STEREO).setModelData(combo, tab.model, idx)
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"stereo": "sbs"})
    ridx = tab.model.index(0, lib.ROTATION)
    spin = tab.table.itemDelegateForColumn(lib.ROTATION).createEditor(tab, None, ridx)
    assert isinstance(spin, QSpinBox) and spin.maximum() == 359


def test_refresh_does_not_clobber_edit(win):
    tab = feed(win, [video()])
    tab.table.setCurrentIndex(tab.model.index(0, lib.TITLE))
    tab.table.edit(tab.model.index(0, lib.TITLE))
    assert tab.editing()
    feed(win, [video(title="Server title")])
    assert cell(tab, 0, lib.TITLE) == "Alpha"
    tab.table.closePersistentEditor(tab.model.index(0, lib.TITLE))
    tab.table.setState(tab.table.State.NoState)
    tab._editor_closed()
    assert cell(tab, 0, lib.TITLE) == "Server title"


def test_refresh_keeps_rows_and_failure_reloads(win):
    tab = feed(win, [video()])
    resets = []
    tab.model.modelReset.connect(lambda: resets.append(1))
    feed(win, [video(size=11)])
    assert not resets and cell(tab, 0, lib.SIZE) == "11 B"
    tab.model.setData(tab.model.index(0, lib.STEREO), "tb")
    win.bridge.failed.emit("bad")
    assert cell(tab, 0, lib.STEREO) == "Mono"
    feed(win, [])
    assert tab.model.rowCount() == 0 and resets
    tab.rescan_button.click()
    assert win.bridge.calls[-1] == ("rescan",)


def test_auto_choice_and_source_tooltip(win):
    tab = feed(win, [video(format_source="filename", projection="180", stereo="sbs")])
    m = tab.model
    assert cell(tab, 0, lib.PROJECTION) == "Auto (180°)"
    assert "file name" in cell(tab, 0, lib.PROJECTION, Qt.ToolTipRole)
    delegate = tab.table.itemDelegateForColumn(lib.PROJECTION)
    idx = m.index(0, lib.PROJECTION)
    combo = delegate.createEditor(tab, None, idx)
    assert combo.itemText(0) == "Auto (detected: 180°)"
    delegate.setEditorData(combo, idx)
    assert combo.currentData() == "auto"
    n = len(win.bridge.calls)
    assert not m.setData(idx, "auto") and len(win.bridge.calls) == n  # already auto
    assert m.setData(idx, "flat")
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"projection": "flat"})
    assert cell(tab, 0, lib.PROJECTION) == "Flat screen"
    assert m.setData(idx, "auto")
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"projection": "auto"})


def test_view_combo_sends_update_video(win):
    tab = feed(win, [video(format_source="filename"), video("b.mp4")])
    assert not tab.view_combo.isEnabled() or tab.selected_video() is None
    tab.table.selectRow(0)
    tab.table.setCurrentIndex(tab.model.index(0, 0))
    assert tab.view_combo.isEnabled() and tab.view_combo.currentData() == "auto"
    assert win.bridge.calls == []  # syncing the combo sends nothing
    tab.view_combo.setCurrentIndex(tab.view_combo.findData("180/sbs"))
    tab._on_view_chosen(tab.view_combo.currentIndex())
    assert win.bridge.calls[-1] == ("update_video", "a.mp4", {"projection": "180", "stereo": "sbs"})
    tab.view_combo.setCurrentIndex(0)
    tab._on_view_chosen(0)
    assert win.bridge.calls[-1][2] == {"projection": "auto", "stereo": "auto"}
    tab.table.setCurrentIndex(tab.model.index(1, 0))
    assert tab.view_combo.currentData() == "360/mono"  # b.mp4: operator-set
    assert not hasattr(win.playback, "view_combo")
