import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QtMsgType, qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QLabel, QPushButton  # noqa: E402

from syncvr.gui import theme  # noqa: E402


def test_stylesheet_applies_without_warnings(qapp):
    messages = []
    previous = qInstallMessageHandler(lambda mode, ctx, msg: messages.append((mode, msg)))
    try:
        theme.apply_theme(qapp)
        button = QPushButton("Play")
        button.setProperty("primary", "true")
        badge = QLabel("playing")
        badge.setObjectName("badge")
        theme.set_property(badge, "state", theme.state_style("playing"))
        button.grab()
        badge.grab()
    finally:
        qInstallMessageHandler(previous)
    assert not [m for m in messages if m[0] != QtMsgType.QtDebugMsg], messages
    assert 'QLabel#badge[state="playing"]' in qapp.styleSheet()
    assert theme.state_style("weird") == "paused"
