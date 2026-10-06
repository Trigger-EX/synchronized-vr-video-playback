"""Operator's local player: follows a headset's desired state with QtMultimedia, optionally shows
the video in a pop-out sphere view that can mirror the headset's view direction."""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional, Protocol

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import proxy as proxymod
from .localsync import LocalSync

try:  # needs PySide6-Addons / OpenGL support
    from .sphereview import SphereView
except ImportError:
    SphereView = None

log = logging.getLogger(__name__)

TICK_MS = 250
NO_FRAME_MS = 5000  # playing this long without a video frame => report a decoder problem
NO_FRAME_TEXT = "no video frames received (codec/decoder problem?)"
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
        self._player.mediaStatusChanged.connect(self._status)

    @staticmethod
    def available():
        try:
            import PySide6.QtMultimedia  # noqa: F401
        except ImportError as e:
            return False, "QtMultimedia is not installed (%s)" % MEDIA_HINT
        return True, ""

    def _frame(self, frame) -> None:
        if not self.on_frame or not frame.isValid():
            return
        try:
            img = frame.toImage()
        except Exception as e:  # noqa: BLE001
            self._report("cannot convert video frame: %s" % e)
            return
        if img.isNull():
            self._report("video frame could not be converted to an image")
            return
        self.on_frame(img)

    def _report(self, text: str) -> None:
        log.error("local playback: %s", text)
        if self.on_error:
            self.on_error(text)

    def _status(self, status) -> None:
        log.info("local playback: media status %s", getattr(status, "name", status))
        if getattr(status, "name", "") == "InvalidMedia":
            self._report("invalid media (unsupported codec/container?)")

    def _error(self, code, text="") -> None:
        log.error("local playback: media error %s: %s", getattr(code, "name", code), text)
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
    _proxyProgress = Signal(str, float)
    _proxyDone = Signal(str, str, str)  # source, proxy path or '', error

    def __init__(self, bridge, content_dir, server_now, backend_factory=None, view_factory=None, parent=None,
                 proxy_job_factory=None):
        super().__init__(parent)
        self._bridge = bridge
        self._content_dir = content_dir
        self._server_now = server_now
        self._backend_factory = backend_factory or (lambda video: QtMediaBackend(video))
        self._view_factory = view_factory or _default_view
        self._injected = backend_factory is not None
        self._view_injected = view_factory is not None
        self._sync = LocalSync()
        self._job_factory = proxy_job_factory or proxymod.default_job_factory
        self._job = None
        self._job_src: Optional[str] = None
        self._proxies: dict = {}  # original path -> ready proxy path
        self._proxy_failed: set = set()
        self._loaded_src: Optional[str] = None
        self._using_proxy = False
        self._proxyProgress.connect(self._on_proxy_progress)
        self._proxyDone.connect(self._on_proxy_done)
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
        self._frames = 0
        self._no_frame_timer = QTimer(self)
        self._no_frame_timer.setSingleShot(True)
        self._no_frame_timer.setInterval(NO_FRAME_MS)
        self._no_frame_timer.timeout.connect(self._check_frames)
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
            self.backend.on_frame = self._on_frame
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
        src = p["path"]
        if src == self._failed:
            return
        if self._job is not None and src != self._job_src:
            self._cancel_proxy()
        path = src
        if src == self._loaded_src and self._loaded:
            path = self._loaded
        elif self.mode == "video":
            path = self._resolve_proxy(src)
            if path is None:
                return  # laptop copy still being prepared
        if path != self._loaded:
            self._failed = None
            self.backend.stop()
            self.backend.load(path)
            self._loaded, self._loaded_src, self._state = path, src, "stopped"
            self._using_proxy = path != src
            self._frames = 0
            self._no_frame_timer.stop()
            p["seek"] = True
            self._sync.reset()
            self._sync.step(self._snapshot, self._focus or "", self._server_now(), self._content_dir, None)
            self._update_view(os.path.basename(src))
        if p["seek"]:
            self.backend.seek(p["target_pos"])
        if p["mode"] == "play" and self._state != "playing":
            self.backend.play()
            self._state = "playing"
            self._arm_frame_watch()
        elif p["mode"] == "pause" and self._state != "paused":
            self.backend.pause()
            self._state = "paused"

    def _arm_frame_watch(self) -> None:
        if self.mode == "video" and self._frames == 0 and not self._no_frame_timer.isActive():
            self._no_frame_timer.start()

    def _on_frame(self, image) -> None:
        if self._frames == 0:
            log.info("local playback: first video frame (%s)", getattr(image, "size", lambda: "?")())
            self._set_status("")
        self._frames += 1
        self._no_frame_timer.stop()
        if self.window is not None:
            self.window.view.set_frame(image)

    def _check_frames(self) -> None:
        if self._frames == 0 and self._state == "playing" and self._loaded:
            self._show_error(NO_FRAME_TEXT)

    def _set_status(self, text: str) -> None:
        view = self.window.view if self.window is not None else None
        if view is not None and hasattr(view, "set_status"):
            view.set_status(text)

    def _show_error(self, text: str) -> None:
        log.error("local playback: %s", text)
        self._set_status(text)
        self.error.emit(text)

    # ----------------------------------------------------------- laptop copy

    def _resolve_proxy(self, src: str) -> Optional[str]:
        """Path to play for `src` in video mode: the cached proxy, the original (no proxy needed or
        possible), or None while the proxy is being made."""
        if src in self._proxies and os.path.isfile(self._proxies[src]):
            return self._proxies[src]
        if src in self._proxy_failed:
            return src
        name = os.path.basename(src)
        v = next((x for x in (self._snapshot or {}).get("library") or [] if x.get("name") == name), None) or {}
        plan = proxymod.proxy_plan(v.get("width"), v.get("height"), v.get("projection"), v.get("stereo"))
        if plan is None:
            return src
        dst = proxymod.cache_path(self._content_dir, name, v.get("size") or os.path.getsize(src),
                                  v.get("mtime") or os.path.getmtime(src), plan)
        if dst.is_file():
            self._proxies[src] = str(dst)
            return str(dst)
        if self._job_src == src:
            return None
        self._cancel_proxy()
        probe = v.get("probe") or {}
        fps = (probe.get("video") or {}).get("fps")
        acodec = (probe.get("audio") or {}).get("codec")
        args = proxymod.ffmpeg_args(src, dst, plan, fps, acodec)
        log.info("local playback: preparing laptop copy %s -> %dx%d", name, plan[0], plan[1])
        self._set_status("Preparing laptop copy\u2026 0%")
        self._job_src = src
        try:
            self._job = self._job_factory(
                args, dst, v.get("duration"),
                lambda pct, s=src: self._proxyProgress.emit(s, pct),
                lambda out, err, s=src: self._proxyDone.emit(s, out or "", err or ""))
            self._job.start()
        except Exception as e:  # noqa: BLE001
            self._job, self._job_src = None, None
            self._proxy_unavailable(src, str(e))
            return src
        return None

    def _cancel_proxy(self) -> None:
        job, self._job, self._job_src = self._job, None, None
        if job is not None:
            job.cancel()

    def _proxy_unavailable(self, src: str, why: str) -> None:
        self._proxy_failed.add(src)
        log.error("local playback: laptop copy failed (%s)", why)
        self._set_status("")
        self.error.emit("Laptop copy failed (%s); playing the original file" % why)

    def _on_proxy_progress(self, src: str, pct: float) -> None:
        if src == self._job_src:
            self._set_status("Preparing laptop copy\u2026 %d%%" % int(pct))

    def _on_proxy_done(self, src: str, out: str, err: str) -> None:
        if src != self._job_src:
            return
        self._job, self._job_src = None, None
        if out:
            self._proxies[src] = out
            self._set_status("")
        else:
            self._proxy_unavailable(src, err or "unknown error")
        self.tick()

    def _halt(self) -> None:
        self._cancel_proxy()
        self._no_frame_timer.stop()
        self._frames = 0
        if self._loaded or self._state != "stopped":
            self.backend.stop()
        self._loaded, self._loaded_src, self._state = None, None, "stopped"

    def _fail(self, key: str, text: str) -> None:
        if self._failed == key:
            return
        self._failed = key
        self._halt()
        self._show_error(text)

    def _backend_error(self, text: str) -> None:
        name = os.path.basename(self._loaded or "") or "video"
        self._fail(self._loaded_src or self._loaded or text, "Cannot play %s on this computer: %s" % (name, text))

    # ------------------------------------------------------------------ view

    def _ensure_window(self) -> None:
        if self.window is None:
            self.window = LocalPlayerWindow(self._view_factory())
            if hasattr(self.window.view, "set_status"):
                self.window.view.set_status("Waiting for video\u2026")
            problem = getattr(self.window.view, "problem", None)
            if problem is not None:
                problem.connect(lambda r: self.error.emit("Video view: %s (using slower CPU rendering)" % r))
            self.window.closed.connect(self._on_window_closed)

    def _update_view(self, name: str) -> None:
        if self.window is None:
            return
        v = next((x for x in (self._snapshot or {}).get("library") or [] if x.get("name") == name), None) or {}
        stereo = "mono" if self._using_proxy else v.get("stereo")  # the laptop copy is left eye only
        key = (v.get("projection"), stereo, v.get("rotation"))
        if key != self._view_key:
            self._view_key = key
            self.window.view.set_view(v.get("projection") or "360", stereo or "mono",
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
        self._cancel_proxy()
        self._proxies.clear()
        self._no_frame_timer.stop()
        self._frames = 0
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
        self._loaded, self._loaded_src, self._failed, self._state, self._view_key = None, None, None, "stopped", None
        self._using_proxy = False
        self._proxy_failed.clear()
        self._sync.reset()
        self.mode = None
