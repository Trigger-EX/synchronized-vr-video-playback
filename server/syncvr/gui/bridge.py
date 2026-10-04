"""Connects Qt widgets to the controller, which lives on the server's event loop in another thread.

Widgets never touch the controller: they receive snapshot dicts through ``state`` and call bridge methods.
"""

from PySide6.QtCore import QObject, QTimer, Signal

DIRTY_POLL_MS = 250
FALLBACK_MS = 1000


class Bridge(QObject):
    state = Signal(object)          # controller snapshot (dict)
    result = Signal(str, object)    # action, result dict of a command or admin call
    failed = Signal(str)            # error message
    stopped = Signal()              # the server thread is no longer running

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
        self._run(action, lambda c: c.execute(action, dict(params, targets=targets)))

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

    def rescan(self) -> None:
        self._run("rescan", _rescan)


def _rescan(controller) -> dict:
    controller.rescan()
    return {"videos": len(controller.library.videos)}
