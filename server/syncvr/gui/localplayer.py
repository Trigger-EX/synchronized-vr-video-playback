"""Operator's local player: follows a headset's desired state with QtMultimedia, optionally shows
the video in a pop-out sphere view that can mirror the headset's view direction."""
from __future__ import annotations

import os
from typing import Callable, Optional, Protocol

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .localsync import LocalSync

try:  # needs PySide6-Addons / OpenGL support
    from .sphereview import SphereView
except ImportError:
    SphereView = None

TICK_MS = 250
POSE_MS = 100  # 10 Hz
MEDIA_HINT = "install the media extra (pip install 'syncvr[media]')"


class MediaBackend(Protocol):
    """What LocalPlayer needs from a player. Callbacks are set by LocalPlayer after construction."""
    on_error: Optional[Callable[[str], None]]
    on_frame: Optional[Callable[[QImage], None]]

    def load(self, path: str) -> None: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def seek(self, seconds: float) -> None: ...
    def position(self) -> Optional[float]: ...
    def close(self) -> None: ...


class QtMediaBackend:
    def __init__(self, video: bool = True):
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        self._url = QUrl
        self.on_error = None
        self.on_frame = None
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._sink = None
        if video:
            self._sink = QVideoSink()
            self._sink.videoFrameChanged.connect(self._frame)
            self._player.setVideoSink(self._sink)
        self._player.errorOccurred.connect(self._error)

    @staticmethod
    def available():
        try:
            import PySide6.QtMultimedia  # noqa: F401
        except ImportError as e:
            return False, "QtMultimedia is not installed (%s)" % MEDIA_HINT
        return True, ""

    def _frame(self, frame) -> None:
        if self.on_frame and frame.isValid():
            self.on_frame(frame.toImage())

    def _error(self, _code, text="") -> None:
        if self.on_error:
            self.on_error(text or "media playback error")

    def load(self, path):
        self._player.setSource(self._url.fromLocalFile(path))

    def play(self):
        self._player.play()

    def pause(self):
        self._player.pause()

    def stop(self):
        self._player.stop()

    def seek(self, seconds):
        self._player.setPosition(int(max(seconds, 0) * 1000))

    def position(self):
        return self._player.position() / 1000.0

    def close(self):
        self._player.stop()
        self._player.setSource(self._url())


class LocalPlayerWindow(QWidget):
    closed = Signal()

    def __init__(self, view: QWidget, title: str = "SyncVR local playback"):
        super().__init__()
        self.view = view
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(view)
        self.resize(960, 540)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        event.accept()


def _default_view():
    return SphereView()


class LocalPlayer(QObject):
    """mode None | 'video' | 'audio'. Feed it with update(); it ticks every 250 ms."""
    error = Signal(str)
    windowClosed = Signal()
    poseReceived = Signal(object)

    def __init__(self, bridge, content_dir, server_now, backend_factory=None, view_factory=None, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._content_dir = content_dir
        self._server_now = server_now
        self._backend_factory = backend_factory or (lambda video: QtMediaBackend(video))
        self._view_factory = view_factory or _default_view
        self._injected = backend_factory is not None
        self._view_injected = view_factory is not None
        self._sync = LocalSync()
        self.mode: Optional[str] = None
        self.backend = None
        self.window: Optional[LocalPlayerWindow] = None
        self._snapshot = None
        self._focus: Optional[str] = None
        self._follow: Optional[str] = None
        self._loaded: Optional[str] = None
        self._failed: Optional[str] = None  # path (or error text) not retried until the video changes
        self._state = "stopped"
        self._view_key = None
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._pose_timer = QTimer(self)
        self._pose_timer.setInterval(POSE_MS)
        self._pose_timer.timeout.connect(self._poll_pose)
        self.poseReceived.connect(self._on_pose)

    # ---------------------------------------------------------- availability

    def availability(self) -> dict:
        ok, reason = (True, "") if self._injected else QtMediaBackend.available()
        video_ok, video_reason = ok, reason
        if ok and not self._view_injected and SphereView is None:
            video_ok, video_reason = False, "OpenGL view unavailable (%s)" % MEDIA_HINT
        return {"video": (video_ok, video_reason), "audio": (ok, reason)}

    # ---------------------------------------------------------------- config

    def set_mode(self, mode: Optional[str]) -> None:
        if mode not in (None, "video", "audio"):
            raise ValueError(mode)
        if mode == self.mode:
            return
        self._teardown()
        self.mode = mode
        if mode is None:
            return
        try:
            self.backend = self._backend_factory(mode == "video")
        except Exception as e:  # missing multimedia at runtime
            self.mode = None
            self.error.emit("Local playback unavailable: %s" % e)
            return
        self.backend.on_error = self._backend_error
        if mode == "video":
            self._ensure_window()
            self.backend.on_frame = self.window.view.set_frame
            self.window.show()
        self._timer.start()
        self._apply_follow()
        self.tick()

    def set_follow(self, device_id: Optional[str]) -> None:
        if self.mode != "video":
            device_id = None
        if device_id == self._follow:
            return
        if self._follow:
            self._bridge.unfollow()
        self._follow = device_id
        self._apply_follow()

    def update(self, snapshot, focus_id: Optional[str]) -> None:
        self._snapshot = snapshot
        self._focus = focus_id

    def stop(self) -> None:
        self.set_follow(None)
        self.set_mode(None)

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        if self.backend is None:
            return
        pos = self.backend.position() if self._loaded else None
        p = self._sync.step(self._snapshot, self._focus or "", self._server_now(), self._content_dir, pos)
        if p["mode"] == "stop":
            self._halt()
            self._failed = None
            return
        if "error" in p:
            self._fail(p["error"], p["error"])
            return
        path = p["path"]
        if path == self._failed:
            return
        if path != self._loaded:
            self._failed = None
            self.backend.stop()
            self.backend.load(path)
            self._loaded, self._state = path, "stopped"
            p["seek"] = True
            self._sync.reset()
            self._sync.step(self._snapshot, self._focus or "", self._server_now(), self._content_dir, None)
            self._update_view(os.path.basename(path))
        if p["seek"]:
            self.backend.seek(p["target_pos"])
        if p["mode"] == "play" and self._state != "playing":
            self.backend.play()
            self._state = "playing"
        elif p["mode"] == "pause" and self._state != "paused":
            self.backend.pause()
            self._state = "paused"

    def _halt(self) -> None:
        if self._loaded or self._state != "stopped":
            self.backend.stop()
        self._loaded, self._state = None, "stopped"

    def _fail(self, key: str, text: str) -> None:
        if self._failed == key:
            return
        self._failed = key
        self._halt()
        self.error.emit(text)

    def _backend_error(self, text: str) -> None:
        name = os.path.basename(self._loaded or "") or "video"
        self._fail(self._loaded or text, "Cannot play %s on this computer: %s" % (name, text))

    # ------------------------------------------------------------------ view

    def _ensure_window(self) -> None:
        if self.window is None:
            self.window = LocalPlayerWindow(self._view_factory())
            self.window.closed.connect(self._on_window_closed)

    def _update_view(self, name: str) -> None:
        if self.window is None:
            return
        v = next((x for x in (self._snapshot or {}).get("library") or [] if x.get("name") == name), None) or {}
        key = (v.get("projection"), v.get("stereo"), v.get("rotation"))
        if key != self._view_key:
            self._view_key = key
            self.window.view.set_view(v.get("projection") or "360", v.get("stereo") or "mono",
                                      v.get("rotation") or 0.0)

    def _on_window_closed(self) -> None:
        self.windowClosed.emit()

    # ---------------------------------------------------------------- follow

    def _apply_follow(self) -> None:
        if self._follow and self.mode == "video":
            self._bridge.follow(self._follow)
            self._pose_timer.start()
        else:
            self._pose_timer.stop()

    def _poll_pose(self) -> None:
        if self._follow:
            self._bridge.pose(self._follow, self.poseReceived.emit)  # called on the server thread

    def _on_pose(self, pose) -> None:
        if pose and self._follow and self.window is not None:
            self.window.view.set_pose(pose.get("yaw", 0.0), pose.get("pitch", 0.0), pose.get("roll", 0.0))

    # -------------------------------------------------------------- teardown

    def _teardown(self) -> None:
        self._timer.stop()
        if self._follow:
            self._bridge.unfollow()
            self._follow = None
        self._pose_timer.stop()
        if self.backend is not None:
            self.backend.on_frame = self.backend.on_error = None
            self.backend.close()
            self.backend = None
        if self.window is not None:
            self.window.closed.disconnect(self._on_window_closed)
            self.window.close()
            self.window.deleteLater()
            self.window = None
        self._loaded, self._failed, self._state, self._view_key = None, None, "stopped", None
        self._sync.reset()
        self.mode = None
