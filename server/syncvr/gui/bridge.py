"""Connects Qt widgets to the controller, which lives on the server's event loop in another thread.

Widgets never touch the controller: they receive snapshot dicts through ``state`` and call bridge methods.
"""

import threading

from PySide6.QtCore import QObject, QTimer, Signal

from ..controller import ADDRESS_ACTIONS

DIRTY_POLL_MS = 250
FALLBACK_MS = 1000


class Bridge(QObject):
    state = Signal(object)          # controller snapshot (dict)
    result = Signal(str, object)    # action, result dict of a command or admin call
    failed = Signal(str)            # error message
    stopped = Signal()              # the server thread is no longer running
    previewed = Signal(str, object, object)  # action, controller.preview() result, the request (params + targets)

    def __init__(self, thread, parent=None):
        super().__init__(parent)
        self._thread = thread
        self._dirty = True
        self._pending = None
        self._closed = False
        self._since_pull = 0
        self._timer = QTimer(self)
        self._timer.setInterval(DIRTY_POLL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._thread.call(lambda c: c.add_listener(self._mark_dirty))
        self._timer.start()
        self.refresh()

    def close(self) -> None:
        self._closed = True
        self._timer.stop()

    def _mark_dirty(self) -> None:  # runs on the server loop thread
        self._dirty = True

    def _tick(self) -> None:
        self._since_pull += DIRTY_POLL_MS
        if self._dirty or self._since_pull >= FALLBACK_MS:
            self.refresh()

    def refresh(self) -> None:
        if self._closed or self._pending is not None:
            return
        if not self._thread.running:
            self._closed = True
            self._timer.stop()
            self.stopped.emit()
            return
        self._dirty = False
        self._since_pull = 0
        fut = self._thread.snapshot_async()
        if fut is None:
            return
        self._pending = fut
        fut.add_done_callback(self._snapshot_done)

    def _snapshot_done(self, fut) -> None:  # server loop thread; signals are queued to the GUI thread
        try:
            snap = fut.result()
        except Exception:
            snap = None
        self._pending = None
        if snap is not None and not self._closed:
            self.state.emit(snap)

    def _run(self, label: str, fn, *args) -> None:
        fut = self._thread.call(fn, *args)

        def done(f):
            try:
                res = f.result()
            except Exception as exc:
                self.failed.emit(str(exc) or exc.__class__.__name__)
                return
            if not self._closed:
                self.result.emit(label, res if isinstance(res, dict) else {})
        fut.add_done_callback(done)

    def command(self, action: str, targets, **params) -> None:
        """Run a controller action on `targets` ("all" or a list of headset ids)."""
        if action in ADDRESS_ACTIONS:  # adb jobs: `adb devices` is fetched off the server loop first
            self.confirmed_command(action, targets, **params)
            return
        self._run(action, lambda c: c.execute(action, dict(params, targets=targets)))

    def _with_adb_listing(self, fn, done) -> None:
        """Call ``fn(controller, adb_devices)`` on the server loop, with `adb devices` fetched off it first."""
        def work():
            try:
                fleet = self._thread.call(lambda c: c.fleet).result(10)
                listed = fleet.listed()
                res = self._thread.call(lambda c: fn(c, listed)).result(30)
            except Exception as exc:
                if not self._closed:
                    self.failed.emit(str(exc) or exc.__class__.__name__)
                return
            if not self._closed:
                done(res)
        threading.Thread(target=work, name="syncvr-gui-adb", daemon=True).start()

    def scan(self, cidr: str) -> None:
        """Probe a small private subnet for adb and connect known headsets; `adb devices` is fetched off the loop."""
        self._with_adb_listing(lambda c, listed: c.scan(cidr, listed=listed),
                               lambda res: self.result.emit("scan", res if isinstance(res, dict) else {}))

    def preview(self, action: str, targets, **params) -> None:
        """Ask the server what `action` would hit; answered by the `previewed` signal."""
        request = dict(params, targets=targets)
        self._with_adb_listing(lambda c, listed: c.preview(dict(request, action=action), listed=listed),
                               lambda res: self.previewed.emit(action, res, request))

    def confirmed_command(self, action: str, targets, **params) -> None:
        """Run a confirm-required action (params carry `confirm` from the preview)."""
        self._with_adb_listing(lambda c, listed: c.execute(action, dict(params, targets=targets), listed=listed),
                               lambda res: self.result.emit(action, res if isinstance(res, dict) else {}))

    def set_feature_tested(self, key: str, tested: bool) -> None:
        self._run("set_feature_tested", lambda c: c.set_feature_tested(key, tested))

    def set_watchdog(self, name: str, **body) -> None:
        """POST /api/watchdogs/{name} equivalent: ``enabled``, ``armed``, ``cfg``, ``testing``."""
        self._run("watchdog", lambda c: c.set_watchdog(name, body) and None)

    def watchdog_test_pattern(self, name: str, **body) -> None:
        """Test (or with ``confirm=True`` confirm) a watchdog pattern against a snapshot folder."""
        self._run("watchdog_pattern", lambda c: c.watchdog_test_pattern(name, body))

    def set_show_mode(self, enabled: bool) -> None:
        self._run("set_show_mode", lambda c: c.set_show_mode(enabled))

    def update_device(self, device_id: str, changes: dict) -> None:
        self._run("update_device", lambda c: c.update_device(device_id, changes) and None)

    def forget_device(self, device_id: str) -> None:
        self._run("forget_device", lambda c: c.forget_device(device_id))

    def update_settings(self, changes: dict) -> None:
        self._run("update_settings", lambda c: c.update_settings(changes) and None)

    def update_video(self, name: str, changes: dict) -> None:
        self._run("update_video", lambda c: c.update_video(name, changes) and None)

    def set_max_downloads(self, value: int) -> None:
        self._run("set_max_downloads", lambda c: c.set_max_downloads(value))

    def follow(self, device_id: str) -> None:
        """Start streaming this headset's view direction (renewed by the server until unfollow)."""
        self._run("follow", lambda c: c.follow(device_id))

    def unfollow(self) -> None:
        self._run("unfollow", lambda c: c.unfollow())

    def pose(self, device_id: str, cb) -> None:
        """Call cb(latest {yaw,pitch,roll,t} or None) from the server thread (not the GUI thread:
        forward it through a Signal). Does not emit result/failed and never marks state dirty."""
        fut = self._thread.call(lambda c: c.pose_of(device_id))

        def done(f):
            try:
                value = f.result()
            except Exception:
                value = None
            if not self._closed:
                cb(value)
        fut.add_done_callback(done)

    def rescan(self) -> None:
        self._run("rescan", _rescan)


def _rescan(controller) -> dict:
    controller.rescan()
    return {"videos": len(controller.library.videos)}
