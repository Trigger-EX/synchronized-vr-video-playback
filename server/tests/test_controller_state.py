from syncvr.controller import Controller
from syncvr.library import Library


class FakeConn:
    remote_ip = "10.0.0.2"
    http_port = 8080

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass

    def content_url(self, name):
        return f"http://10.0.0.2/content/{name}"


def make_controller(content_dir, state=None):
    lib = Library(content_dir)
    lib.scan()
    c = Controller(lib)
    if state:
        c.load(state)
    return c


def connect(c, device_id="hs1"):
    conn = FakeConn()
    c.headset_connected({"device_id": device_id}, conn)
    return conn


def test_pending_download_survives_restart(content_dir):
    c = make_controller(content_dir)
    connect(c)
    c.dirty = False
    c.distributor.max_concurrent = 0  # keep the job queued instead of sending it
    c.execute("sync_content", {"targets": ["hs1"], "delete_others": True})
    assert c.dirty
    state = c.dump()
    assert set(state["downloads"]["hs1"]["files"]) == set(c.library.videos)
    assert state["downloads"]["hs1"]["delete_others"] is True

    c2 = make_controller(content_dir, state)
    conn = FakeConn()
    c2.headset_connected({"device_id": "hs1"}, conn)
    sync = [m for m in conn.sent if m["type"] == "sync_content"]
    assert len(sync) == 1 and sync[0]["delete_others"] is True
    assert {f["name"] for f in sync[0]["files"]} == set(c.library.videos)
    assert "hs1" in c2.distributor.active


def test_active_job_is_dumped_and_cancel_clears_it(content_dir):
    c = make_controller(content_dir)
    connect(c)
    c.execute("sync_content", {"targets": ["hs1"]})
    assert "hs1" in c.distributor.active
    assert "hs1" in c.dump()["downloads"]
    assert "started" not in c.dump()["downloads"]["hs1"]
    c.dirty = False
    c.execute("cancel_downloads", {"targets": ["hs1"]})
    assert c.dirty
    assert c.dump()["downloads"] == {}


def test_load_ignores_downloads_for_unknown_devices(content_dir):
    state = {"downloads": {"ghost": {"files": ["a.mp4"], "delete_others": False}}}
    c = make_controller(content_dir, state)
    assert not c.distributor.queue
