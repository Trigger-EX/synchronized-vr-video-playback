"""Transport, volume, messaging and content-push controls for the targeted headsets."""

import sys

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from . import format as fmt

SEEK_STEPS = (-30, -10, 10, 30)
CLOCK_MS = 250
SEEK_RANGE = 1000


class MediaInstaller(QThread):
    """Runs bootstrap.install_media off the GUI thread; emits done(ok)."""
    done = Signal(bool)

    def __init__(self, parent=None, install=None):
        super().__init__(parent)
        self._install = install

    def run(self) -> None:
        try:
            if self._install is None:
                from .. import bootstrap
                self._install = bootstrap.install_media
            ok = bool(self._install())
        except Exception:
            ok = False
        self.done.emit(ok)


def can_offer_media_install() -> bool:
    if getattr(sys, "frozen", False):
        return False
    try:
        from .. import bootstrap
        return bootstrap.running_from_venv()
    except Exception:
        return False


class PlaybackPanel(QWidget):
    localChanged = Signal()

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._video_touched = False  # until the operator picks a video, follow what is loaded
        self._library_key = None
        self._seek_dragging = False
        self._volume_dragging = False
        self._updating = False

        self.target_label = QLabel("")
        self.target_label.setObjectName("target")
        self.video_combo = QComboBox()
        self.video_combo.setMinimumWidth(240)
        self.video_combo.activated.connect(lambda _i: setattr(self, "_video_touched", True))
        self.now_playing = QLabel("Nothing loaded")
        self.now_playing.setObjectName("now")
        self.pos_label = QLabel("0:00")
        self.pos_label.setObjectName("time")
        self.dur_label = QLabel("0:00")
        self.dur_label.setObjectName("time")
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, SEEK_RANGE)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seek_dragging", True))
        self.seek.sliderMoved.connect(self._on_seek_moved)
        self.seek.sliderReleased.connect(self._on_seek_released)
        self.seek.valueChanged.connect(self._on_seek_changed)

        self.buttons = {}
        transport = QHBoxLayout()
        for key, text, handler in (("load", "Load", lambda: self._with_video("load")),
                                   ("play", "Play", lambda: self._with_video("play")),
                                   ("pause", "Pause", lambda: self.send("pause")),
                                   ("stop", "Stop", lambda: self.send("stop")),
                                   ("restart", "Restart", lambda: self._with_video("play", pos=0)),
                                   ("resync", "Resync", lambda: self.send("resync"))):
            transport.addWidget(self._button(key, text, handler))
        for delta in SEEK_STEPS:
            transport.addWidget(self._button("seek%+d" % delta, "%+ds" % delta, lambda d=delta: self.send("seek", delta=d)))

        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setFixedWidth(160)
        self.volume_label = QLabel("100%")
        self.volume.sliderPressed.connect(lambda: setattr(self, "_volume_dragging", True))
        self.volume.sliderReleased.connect(self._on_volume_released)
        self.volume.valueChanged.connect(self._on_volume_changed)
        self.message_edit = QLineEdit()
        self.message_edit.setPlaceholderText("Message to show in the headsets")
        self.message_edit.returnPressed.connect(self.send_message)

        extras = QHBoxLayout()
        extras.addWidget(QLabel("Volume"))
        extras.addWidget(self.volume)
        extras.addWidget(self.volume_label)
        extras.addWidget(self._button("recenter", "Recenter", lambda: self.send("recenter")))
        extras.addWidget(self._button("identify", "Identify", lambda: self.send("identify")))
        extras.addWidget(self.message_edit, 1)
        extras.addWidget(self._button("message", "Send", self.send_message))

        content = QHBoxLayout()
        content.addWidget(self._button("push", "Push video", lambda: self._with_video("sync_content", pick="videos")))
        content.addWidget(self._button("push_all", "Push all", lambda: self.send("sync_content", videos="all")))
        content.addWidget(self._button("cancel_downloads", "Stop downloads", lambda: self.send("cancel_downloads")))
        content.addWidget(self._button("delete", "Delete video", self.delete_video))
        content.addStretch(1)
        self.local_video = QCheckBox("Play on this computer")
        self.local_audio = QCheckBox("Audio only on this computer")
        self.local_follow = QCheckBox("Follow headset view")
        avail = window.local_player.availability()
        for box, key in ((self.local_video, "video"), (self.local_audio, "audio")):
            ok, reason = avail[key]
            box.setEnabled(ok)
            box.setToolTip("" if ok else "Unavailable: " + reason)
            box.toggled.connect(lambda on, b=box: self._on_local_toggled(b, on))
            content.addWidget(box)
        self.media_button = None
        self._media_installer = None
        if not (avail["video"][0] and avail["audio"][0]) and can_offer_media_install():
            self.media_button = QPushButton("Install media support (~200 MB)")
            self.media_button.setToolTip("Downloads PySide6-Addons (QtMultimedia) into SyncVR's private environment")
            self.media_button.clicked.connect(lambda _c=False: self.install_media())
            content.addWidget(self.media_button)
        self.local_follow.setEnabled(False)
        self.local_follow.setToolTip("Mirror the selected headset's view direction in the local video window")
        self.local_follow.toggled.connect(lambda _on: self.localChanged.emit())
        content.addWidget(self.local_follow)

        top = QHBoxLayout()
        top.addWidget(self.target_label, 1)
        top.addWidget(QLabel("Video"))
        top.addWidget(self.video_combo)
        seek_row = QGridLayout()
        seek_row.addWidget(self.now_playing, 0, 0, 1, 3)
        seek_row.addWidget(self.pos_label, 1, 0)
        seek_row.addWidget(self.seek, 1, 1)
        seek_row.addWidget(self.dur_label, 1, 2)

        box = QGroupBox("PLAYBACK")
        box.setObjectName("playback")
        lay = QVBoxLayout(box)
        for part in (top, seek_row, transport, extras, content):
            lay.addLayout(part)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

        self._timer = QTimer(self)
        self._timer.setInterval(CLOCK_MS)
        self._timer.timeout.connect(self.update_clock)
        self._timer.start()
        self.update_state()

    def _button(self, key, text, handler) -> QPushButton:
        b = QPushButton(text)
        b.clicked.connect(lambda _checked=False: handler())
        self.buttons[key] = b
        if key == "play":
            b.setProperty("primary", "true")
        elif key == "delete":
            b.setProperty("danger", "true")
        return b

    def _on_local_toggled(self, box, on) -> None:
        other = self.local_audio if box is self.local_video else self.local_video
        if on and other.isChecked():
            other.blockSignals(True)
            other.setChecked(False)
            other.blockSignals(False)
        self.local_follow.setEnabled(self.local_video.isChecked() and self.local_video.isEnabled())
        if not self.local_follow.isEnabled():
            self.local_follow.setChecked(False)
        self.localChanged.emit()

    def install_media(self, installer=None) -> None:
        if self._media_installer is not None and self._media_installer.isRunning():
            return
        self.media_button.setEnabled(False)
        self._window.statusBar().showMessage("Installing media support (~200 MB), this can take a few minutes...")
        self._media_installer = MediaInstaller(self, installer)
        self._media_installer.done.connect(self._on_media_done)
        self._media_installer.start()

    def _on_media_done(self, ok: bool) -> None:
        bar = self._window.statusBar()
        if not ok:
            self.media_button.setEnabled(True)
            bar.showMessage("Media support install failed; check your internet connection and try again.", 15000)
            return
        bar.showMessage("Media support installed. Restart SyncVR to use it.", 0)
        self.media_button.setText("Restart SyncVR to finish")
        if QMessageBox.question(self, "SyncVR", "Media support is installed. Restart SyncVR now?") == QMessageBox.Yes:
            self.restart_app()

    def restart_app(self) -> None:
        import os
        try:
            os.execv(sys.executable, [sys.executable, "-m", "syncvr"] + sys.argv[1:])
        except OSError as exc:
            self._window.statusBar().showMessage("Could not restart (%s); please restart SyncVR manually." % exc, 15000)

    def local_mode(self):
        return "video" if self.local_video.isChecked() else "audio" if self.local_audio.isChecked() else None

    def follow_device_id(self):
        """Device to follow: the single selected target, else the focus device; None when off."""
        if not (self.local_follow.isChecked() and self.local_follow.isEnabled()):
            return None
        if len(self._window.targets) == 1:
            return self._window.targets[0]
        dev = self.focus_device()
        return dev["id"] if dev else None

    def uncheck_local(self) -> None:
        self.local_video.setChecked(False)
        self.local_audio.setChecked(False)

    # ------------------------------------------------------------ commands

    def selected_video(self):
        return self.video_combo.currentData()

    def send(self, action, **params) -> None:
        self._window.bridge.command(action, self._window.target_spec(), **params)

    def _with_video(self, action, pick="video", **params) -> None:
        name = self.selected_video()
        if not name:
            self._window.on_failed("No video in the library")
            return
        self.send(action, **{pick: [name] if pick == "videos" else name}, **params)

    def send_message(self) -> None:
        text = self.message_edit.text().strip()
        self.send("message", text=text, seconds=10 if text else 0)
        self.message_edit.clear()

    def confirm(self, text: str) -> bool:
        return QMessageBox.question(self, "SyncVR", text) == QMessageBox.Yes

    def delete_video(self) -> None:
        name = self.selected_video()
        if not name:
            return
        n = len(self._window.target_devices())
        if self.confirm('Remove "%s" from %d headset(s)?' % (name, n)):
            self.send("delete_content", videos=[name])

    # ------------------------------------------------------------- seeking

    def focus_device(self):
        for dev in self._window.target_devices():
            if (dev.get("desired") or {}).get("mode") in ("playing", "paused"):
                return dev
        return None

    def _seek_to_slider(self) -> None:
        dev = self.focus_device()
        if dev and dev["desired"].get("duration"):
            self.send("seek", pos=self.seek.value() / SEEK_RANGE * dev["desired"]["duration"])

    def _on_seek_moved(self, value) -> None:
        dev = self.focus_device()
        if dev:
            self.pos_label.setText(fmt.fmt_time(value / SEEK_RANGE * (dev["desired"].get("duration") or 0)))

    def _on_seek_released(self) -> None:
        self._seek_dragging = False
        self._seek_to_slider()

    def _on_seek_changed(self, _value) -> None:  # keyboard and click-on-groove
        if not self._updating and not self._seek_dragging:
            self._seek_to_slider()

    def _on_volume_changed(self, value) -> None:
        self.volume_label.setText("%d%%" % value)
        if not self._updating and not self._volume_dragging:
            self.send("volume", value=value / 100)

    def _on_volume_released(self) -> None:
        self._volume_dragging = False
        self.send("volume", value=self.volume.value() / 100)

    # -------------------------------------------------------------- render

    def update_state(self) -> None:
        snap = self._window.snapshot or {}
        devices = snap.get("devices") or []
        library = snap.get("library") or []
        selected = len(self._window.targets)
        self.target_label.setText("Controlling %d selected headset%s" % (selected, "" if selected == 1 else "s")
                                  if selected else "Controlling all %d headsets" % len(devices))
        self._updating = True
        try:
            self._fill_videos(library)
            targets = self._window.target_devices()
            if targets and not self._volume_dragging and not self.volume.hasFocus():
                self.volume.setValue(int(round(targets[0].get("volume", 1.0) * 100)))
            dev = self.focus_device()
            if not self._video_touched and dev and dev["desired"]["video"] in [v["name"] for v in library]:
                self.video_combo.setCurrentIndex(self.video_combo.findData(dev["desired"]["video"]))
        finally:
            self._updating = False
        self.update_clock()

    def _fill_videos(self, library) -> None:
        key = [(v["name"], v.get("title") or v["name"]) for v in library]
        if key == self._library_key:
            return
        self._library_key = key
        current = self.video_combo.currentData()
        self.video_combo.clear()
        for name, title in key:
            self.video_combo.addItem(title, name)
        idx = self.video_combo.findData(current)
        if idx >= 0:
            self.video_combo.setCurrentIndex(idx)

    def update_clock(self) -> None:
        dev = self.focus_device()
        self._updating = True
        try:
            if dev is None:
                self.now_playing.setText("Nothing loaded")
                self.dur_label.setText("0:00")
                if not self._seek_dragging:
                    self.pos_label.setText("0:00")
                    self.seek.setValue(0)
                return
            d = dev["desired"]
            pos = fmt.position_of(d, self._window.server_now())
            dur = d.get("duration")
            vid = next((v for v in (self._window.snapshot or {}).get("library") or [] if v["name"] == d["video"]), None)
            self.now_playing.setText("%s: %s" % ("Playing" if d["mode"] == "playing" else "Paused",
                                                 (vid and vid.get("title")) or (vid and vid["name"]) or d["video"]))
            self.dur_label.setText(fmt.fmt_time(dur))
            if not self._seek_dragging:
                self.pos_label.setText(fmt.fmt_time(pos))
                self.seek.setValue(int(round(pos / dur * SEEK_RANGE)) if dur else 0)
        finally:
            self._updating = False
