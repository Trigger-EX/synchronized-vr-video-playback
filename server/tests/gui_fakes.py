"""Test doubles for the Qt panel. Import only after pytest.importorskip("PySide6.QtWidgets")."""

from PySide6.QtCore import QObject, Signal


class FakeBridge(QObject):
    state = Signal(object)
    result = Signal(str, object)
    failed = Signal(str)
    stopped = Signal()
    previewed = Signal(str, object, object)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.closed = False
        self.next_pose = None

    def command(self, action, targets, **params):
        self.calls.append((action, targets, params))

    def preview(self, action, targets, **params):
        self.calls.append(("preview", action, targets, params))

    def confirmed_command(self, action, targets, **params):
        self.calls.append(("confirmed", action, targets, params))

    def set_feature_tested(self, key, tested):
        self.calls.append(("set_feature_tested", key, tested))

    def update_device(self, device_id, changes):
        self.calls.append(("update_device", device_id, changes))

    def forget_device(self, device_id):
        self.calls.append(("forget_device", device_id))

    def update_settings(self, changes):
        self.calls.append(("update_settings", changes))

    def update_video(self, name, changes):
        self.calls.append(("update_video", name, changes))

    def set_max_downloads(self, value):
        self.calls.append(("set_max_downloads", value))

    def follow(self, device_id):
        self.calls.append(("follow", device_id))

    def unfollow(self):
        self.calls.append(("unfollow",))

    def pose(self, device_id, cb):
        self.calls.append(("pose", device_id))
        cb(self.next_pose)

    def rescan(self):
        self.calls.append(("rescan",))

    def refresh(self):
        pass

    def close(self):
        self.closed = True


class Cfg:
    content_dir = "."


def make_snapshot(**over):
    snap = {"server": {"name": "SyncVR", "time": 1000.0, "http_port": 8080, "addresses": ["192.168.1.5"]},
            "settings": {}, "devices": [], "library": [], "downloads": {"active": [], "queued": [], "max_concurrent": 4},
            "events": []}
    snap.update(over)
    return snap


def make_device(dev_id="a", online=True, **over):
    dev = {"id": dev_id, "label": dev_id, "name": "", "group": "", "online": online, "status": {},
           "inventory": {}, "desired": None, "volume": 1.0}
    dev.update(over)
    return dev
