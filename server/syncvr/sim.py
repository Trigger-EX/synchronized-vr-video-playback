"""Simulated headsets: load-test the server and measure sync accuracy without hardware.

Each simulated headset speaks the real protocol, runs the reference
``SyncEngine`` against a fake video decoder with realistic imperfections
(clock offset and skew, start latency, seek time, rate error) and really
downloads content over HTTP.

    python -m syncvr sim --server 127.0.0.1 --count 20
"""

import argparse
import asyncio
import json
import logging
import os
import random
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from . import __version__
from .analysis import sha256_file
from .protocol import (DEFAULT_DISCOVERY_PORT, DEFAULT_TCP_PORT, PROTOCOL_VERSION, SERVICE_NAME, decode,
                       encode, is_safe_filename)
from .sync_engine import ClockSync, SyncEngine

log = logging.getLogger(__name__)

TICK_HZ = 72.0  # Oculus Go refresh rate


class SimPlayer:
    """A fake video decoder. Its notion of media time advances with *real*
    time (``real_clock``) scaled by its rate and a crystal error, and it
    takes a while to load, start and seek - like the real thing."""

    def __init__(self, rng: random.Random, real_clock=time.monotonic, rate_error_ppm: float = 0.0,
                 start_latency: float = 0.08, seek_time: float = 0.2, load_time: float = 0.4,
                 jitter: float = 0.15, can_set_rate: bool = True):
        self.rng = rng
        self.real = real_clock
        self.rate_error = rate_error_ppm * 1e-6
        # Each delay is the device's typical value, varying by +/- ``jitter`` (fraction) each time.
        self.start_latency = start_latency
        self.seek_time = seek_time
        self.load_time = load_time
        self.jitter = jitter
        self.can_set_rate = can_set_rate
        self.loaded_video: Optional[str] = None
        self.length = 0.0
        self.loop = False
        self.is_prepared = False
        self.error = None
        self._pos = 0.0
        self._t = real_clock()
        self._playing = False
        self._seeking = False
        self._rate = 1.0
        self._events = []
        self._load_gen = 0
        self._start_gen = 0

    def _schedule(self, delay, fn):
        delay *= 1.0 + self.rng.uniform(-self.jitter, self.jitter)
        self._events.append((self.real() + delay, fn))

    def _advance(self):
        now = self.real()
        if self._playing and not self._seeking and self.is_prepared:
            self._pos += (now - self._t) * self._rate * (1.0 + self.rate_error)
            if self.length and self._pos >= self.length:
                if self.loop:
                    self._pos %= self.length
                else:
                    self._pos = self.length
                    self._playing = False
        self._t = now

    def pump(self):
        now = self.real()
        due = sorted((e for e in self._events if e[0] <= now), key=lambda e: e[0])
        self._events = [e for e in self._events if e[0] > now]
        for _, fn in due:
            self._advance()
            fn()

    @property
    def is_seeking(self):
        return self._seeking

    @property
    def is_playing(self):
        return self._playing

    @property
    def time(self):
        self._advance()
        return self._pos

    def load(self, msg):
        self._load_gen += 1
        self._start_gen += 1
        self._events.clear()
        self.loaded_video = msg["video"]
        self.length = msg.get("duration") or 3600.0
        self.loop = bool(msg.get("loop"))
        self.is_prepared = False
        self._playing = self._seeking = False
        self._pos = 0.0
        gen = self._load_gen

        def ready():
            if gen == self._load_gen:
                self.is_prepared = True
        self._schedule(self.load_time, ready)

    def play(self):
        self._advance()
        gen = self._start_gen

        def start():
            if gen == self._start_gen:
                self._advance()
                self._playing = True
        self._schedule(self.start_latency, start)

    def pause(self):
        self._advance()
        self._start_gen += 1  # cancels a pending start
        self._playing = False

    def stop(self):
        self._load_gen += 1
        self._start_gen += 1
        self._events.clear()
        self.loaded_video = None
        self.is_prepared = False
        self._playing = self._seeking = False
        self._pos = 0.0

    def seek(self, t):
        self._advance()
        self._seeking = True
        gen = self._load_gen

        def done():
            if gen == self._load_gen:
                self._pos = max(0.0, t)
                self._seeking = False
        self._schedule(self.seek_time, done)

    def set_rate(self, rate):
        self._advance()
        self._rate = rate

    def set_loop(self, loop):
        self.loop = loop

    def set_external_time(self, t):
        pass  # the simulator has no player-side clock slaving

    def clear_external_time(self):
        pass


class LocalClock:
    """A headset's monotonic clock: arbitrary origin and slightly wrong speed."""

    def __init__(self, offset: float, skew_ppm: float):
        self.offset = offset
        self.skew = skew_ppm * 1e-6
        self.base = time.monotonic()

    def __call__(self) -> float:
        real = time.monotonic()
        return self.offset + self.base + (real - self.base) * (1.0 + self.skew)


class SimHeadset:
    def __init__(self, index: int, server: Optional[str], port: int = DEFAULT_TCP_PORT,
                 download_dir: Optional[Path] = None, seed: Optional[int] = None,
                 discovery_port: int = DEFAULT_DISCOVERY_PORT, realistic: bool = True,
                 throttle_bps: Optional[float] = None):
        self.index = index
        self.throttle_bps = throttle_bps  # cap on download speed, bytes/s (None = unlimited)
        self.server = server
        self.port = port
        self.discovery_port = discovery_port
        self.rng = random.Random(seed if seed is not None else index)
        self.device_id = f"SIM-{index:03d}-{uuid.UUID(int=self.rng.getrandbits(128)).hex[:6]}"
        self.name = ""
        self.download_dir = Path(download_dir or tempfile.mkdtemp(prefix=f"syncvr-sim{index}-"))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if realistic:
            # Arbitrary clock origin, crystal errors, and per-device decoder timing.
            self.clock = LocalClock(self.rng.uniform(-5000, 5000), self.rng.uniform(-80, 80))
            self.player = SimPlayer(self.rng, rate_error_ppm=self.rng.uniform(-300, 300),
                                    start_latency=self.rng.uniform(0.02, 0.2),
                                    seek_time=self.rng.uniform(0.05, 0.5),
                                    load_time=self.rng.uniform(0.2, 1.0))
        else:
            self.clock = LocalClock(0.0, 0.0)
            self.player = SimPlayer(self.rng, start_latency=0, seek_time=0, load_time=0)
        self.sync = ClockSync()
        self.engine = SyncEngine(self.player, self._queue_event)
        self.volume = 1.0
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = asyncio.Event()
        self.download: Optional[dict] = None
        self.download_task: Optional[asyncio.Task] = None
        self.job = None  # id of the current sync_content job, echoed in downloads_finished
        self._ping_id = 0
        self._pending_events = []
        self.messages = []  # everything received, for tests

    # ------------------------------------------------------------ helpers

    def server_now(self) -> float:
        return self.sync.server_time(self.clock())

    def true_error(self) -> Optional[float]:
        """Actual position minus where it should be, using the real server
        clock (only meaningful when the server runs in the same process)."""
        anchor = self.engine.anchor
        if anchor is None or self.engine.state != "playing" or self.engine.pending_start:
            return None
        expected = anchor["pos"] + (time.monotonic() - anchor["at"])
        return self.player.time - expected

    def _queue_event(self, level, message):
        self._pending_events.append({"type": "event", "level": level, "message": message})

    def send(self, msg: dict) -> None:
        if self.writer is not None and not self.writer.is_closing():
            self.writer.write(encode(msg))

    def inventory(self) -> list:
        return [{"name": p.name, "size": p.stat().st_size}
                for p in sorted(self.download_dir.iterdir()) if p.is_file() and not p.name.endswith(".part")]

    # ---------------------------------------------------------- lifecycle

    async def discover(self) -> tuple:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    msg = json.loads(data)
                except ValueError:
                    return
                if msg.get("service") == SERVICE_NAME and not fut.done():
                    fut.set_result((addr[0], int(msg["tcp_port"])))

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", self.discovery_port))
        transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
        try:
            return await fut
        finally:
            transport.close()

    async def run(self) -> None:
        backoff = 0.5
        while True:
            try:
                host, port = (self.server, self.port) if self.server else await self.discover()
                reader, writer = await asyncio.open_connection(host, port)
                backoff = 0.5
                await self._session(reader, writer, host)
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                log.debug("%s: %s", self.device_id, exc)
            self.connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    async def _session(self, reader, writer, host) -> None:
        self.writer = writer
        self.host = host
        self.sync.reset()
        self.send({"type": "hello", "proto": PROTOCOL_VERSION, "device_id": self.device_id,
                   "model": "Simulator", "app_version": __version__, "player": "sim", "serial": self.device_id})
        self.send({"type": "inventory", "files": self.inventory()})
        tasks = [asyncio.create_task(t) for t in (self._pinger(), self._ticker(), self._reporter())]
        try:
            while True:
                line = await _readline(reader, 15)
                if not line:
                    break
                t1 = self.clock()
                self._handle(decode(line), t1)
        finally:
            for t in tasks:
                t.cancel()
            self.writer = None
            writer.close()

    async def _pinger(self) -> None:
        for i in range(10):
            self._ping()
            await asyncio.sleep(0.1)
        while True:
            await asyncio.sleep(2.0)
            self._ping()

    def _ping(self) -> None:
        self._ping_id += 1
        self.send({"type": "time_ping", "id": self._ping_id, "t0": self.clock()})

    async def _ticker(self) -> None:
        while True:
            self.player.pump()
            if self.sync.synced:
                self.engine.update(self.server_now())
            for ev in self._pending_events:
                self.send(ev)
            self._pending_events.clear()
            await asyncio.sleep(1.0 / TICK_HZ)

    async def _reporter(self) -> None:
        while True:
            self.send(self.status())
            await asyncio.sleep(1.0)

    def status(self) -> dict:
        st = self.engine.status(self.server_now())
        st.update({
            "type": "status",
            "battery": round(0.8 - self.index * 0.01, 2),
            "charging": False,
            "temp_c": 31.5,
            "worn": True,
            "storage_free": 20 * 1024 ** 3,
            "volume": self.volume,
            "rtt_ms": round(self.sync.rtt * 1000.0, 1) if self.sync.rtt is not None else None,
            "clock_synced": self.sync.synced,
            "fps": TICK_HZ,
            "download": self.download,
        })
        return st

    # ----------------------------------------------------------- messages

    def _handle(self, msg: dict, t1: float) -> None:
        kind = msg["type"]
        self.messages.append(msg)
        if kind == "time_pong":
            self.sync.add(float(msg["t0"]), float(msg["ts"]), t1)
            if not self.connected.is_set() and len(self.sync.samples) >= 5:
                self.connected.set()
        elif kind == "welcome":
            self.name = msg.get("device_name", "")
            self.http_port = msg.get("http_port")
            self.engine.on_settings(msg.get("settings", {}))
        elif kind == "settings":
            self.engine.on_settings(msg.get("settings", {}))
        elif kind == "device_info":
            self.name = msg.get("device_name", self.name)
        elif kind == "play":
            self.engine.on_play(msg)
        elif kind == "pause":
            self.engine.on_pause(msg)
        elif kind == "stop":
            self.engine.on_stop()
        elif kind == "volume":
            self.volume = float(msg.get("value", 1.0))
        elif kind == "sync_content":
            if self.download_task and not self.download_task.done():
                self.download_task.cancel()
            self.job = msg.get("job")
            self.download_task = asyncio.create_task(self._sync_content(msg))
        elif kind == "cancel_downloads":
            if self.download_task:
                self.download_task.cancel()
        elif kind == "delete_content":
            for name in msg.get("names", []):
                if is_safe_filename(name):
                    (self.download_dir / name).unlink(missing_ok=True)
            self.send({"type": "inventory", "files": self.inventory()})

    async def _sync_content(self, msg: dict) -> None:
        import aiohttp  # only needed when content is actually pushed

        ok, failed = [], []
        job = msg.get("job")
        wanted = {f["name"] for f in msg.get("files", [])}
        try:
            if msg.get("delete_others"):
                for p in self.download_dir.iterdir():
                    if p.is_file() and p.name not in wanted and not p.name.endswith(".part"):
                        p.unlink()
            async with aiohttp.ClientSession() as session:
                for f in msg.get("files", []):
                    name, size = f["name"], int(f["size"])
                    if not is_safe_filename(name):
                        failed.append(name)
                        continue
                    dest = self.download_dir / name
                    expected = f.get("sha256")
                    if dest.exists() and dest.stat().st_size == size:
                        if not expected or await self._verify(dest, expected):
                            ok.append(name)
                            continue
                    try:
                        await self._download(session, f["url"], dest, size)
                        if expected and not await self._verify(dest, expected):
                            # Corrupt download: throw it away and fetch once more.
                            await self._download(session, f["url"], dest, size)
                            if not await self._verify(dest, expected):
                                raise ValueError("checksum mismatch")
                        ok.append(name)
                    except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError) as exc:
                        log.warning("%s: download of %s failed: %s", self.device_id, name, exc)
                        failed.append(name)
        finally:
            if asyncio.current_task() is self.download_task:  # not a superseded job
                self.download = None
            self.send({"type": "inventory", "files": self.inventory()})
        done = {"type": "downloads_finished", "ok": ok, "failed": failed}
        if job is not None:
            done["job"] = job
        self.send(done)

    async def _verify(self, path: Path, expected: str) -> bool:
        """Check a finished file against the server's SHA-256; a bad file is deleted."""
        digest = await asyncio.get_running_loop().run_in_executor(None, sha256_file, path)
        if digest == expected.lower():
            return True
        log.warning("%s: checksum mismatch on %s, discarding", self.device_id, path.name)
        path.unlink(missing_ok=True)
        return False

    async def _download(self, session, url: str, dest: Path, size: int) -> None:
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        progress = self.download = {"name": dest.name, "received": have, "total": size}
        async with session.get(url, headers=headers, timeout=aiohttp_timeout()) as resp:
            if resp.status == 200:
                have = 0
            elif resp.status != 206:
                raise ValueError(f"HTTP {resp.status}")
            with open(part, "ab" if have else "wb") as out:
                async for chunk in resp.content.iter_chunked(64 * 1024 if self.throttle_bps else 256 * 1024):
                    out.write(chunk)
                    out.flush()
                    have += len(chunk)
                    progress["received"] = have
                    if self.throttle_bps:
                        await asyncio.sleep(len(chunk) / self.throttle_bps)
        if have != size:
            raise ValueError(f"size mismatch: got {have}, expected {size}")
        os.replace(part, dest)


async def _readline(reader, timeout: float) -> bytes:
    """reader.readline() with a timeout that cannot swallow a cancellation.

    asyncio.wait_for before Python 3.12 drops a cancel() that lands when the line
    arrives in the same loop step, leaving the simulator running forever.
    """
    task = asyncio.ensure_future(reader.readline())
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise asyncio.TimeoutError()
        return task.result()
    finally:
        task.cancel()


def aiohttp_timeout():
    import aiohttp
    return aiohttp.ClientTimeout(total=None, sock_read=30)


async def run_fleet(args) -> None:
    headsets = [
        SimHeadset(i + 1, args.server, args.port,
                   Path(args.download_dir) / f"sim{i + 1:03d}" if args.download_dir else None,
                   discovery_port=args.discovery_port)
        for i in range(args.count)
    ]
    tasks = [asyncio.create_task(h.run()) for h in headsets]
    print(f"started {len(headsets)} simulated headset(s); Ctrl+C to stop")
    try:
        while True:
            await asyncio.sleep(5)
            playing = [h for h in headsets if h.engine.state == "playing" and h.engine.drift is not None]
            if playing:
                drifts = sorted(abs(h.engine.drift) * 1000 for h in playing)
                print(f"{len(playing)} playing; reported drift median {drifts[len(drifts) // 2]:.1f} ms, "
                      f"max {drifts[-1]:.1f} ms")
    finally:
        for t in tasks:
            t.cancel()


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--server", help="server address (default: find it via UDP discovery)")
    p.add_argument("--port", type=int, default=DEFAULT_TCP_PORT, help="server headset port")
    p.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT)
    p.add_argument("--count", type=int, default=5, help="number of simulated headsets")
    p.add_argument("--download-dir", help="where simulated headsets store content (default: temp dirs)")
