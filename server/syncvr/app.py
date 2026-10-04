"""Wires the pieces together into one server process."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from aiohttp import web

from . import __version__
from .analysis import CACHE_FILE, ContentAnalyzer
from .controller import Controller
from .discovery import DiscoveryBeacon, default_broadcast_addresses, local_ipv4_addresses
from .headset_server import HeadsetServer
from .library import Library
from .protocol import (DEFAULT_DISCOVERY_PORT, DEFAULT_HTTP_PORT, DEFAULT_TCP_PORT, PROTOCOL_VERSION,
                       SERVICE_NAME)
from .store import StateStore
from .web import WebApp

log = logging.getLogger(__name__)

SAVE_INTERVAL_S = 2.0
RESCAN_INTERVAL_S = 30.0


@dataclass
class ServerConfig:
    content_dir: Path = Path("content")
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    http_port: int = DEFAULT_HTTP_PORT
    tcp_port: int = DEFAULT_TCP_PORT
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    broadcast: Optional[List[str]] = None
    discovery: bool = True
    name: str = "SyncVR"
    public_host: str = ""  # address headsets use for downloads; "" = the one they connected to
    password: str = ""
    max_downloads: Optional[int] = None  # None: keep the saved value (default 4)


class SyncServer:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.store = StateStore(Path(config.data_dir) / "state.json")
        self.analyzer = ContentAnalyzer(Path(config.data_dir) / CACHE_FILE)
        if not self.analyzer.available:
            log.warning("ffprobe not found: videos will not be checked against the Go's limits "
                        "(install ffmpeg to enable). Checksums still work.")
        self.library = Library(Path(config.content_dir), analyzer=self.analyzer)
        self.controller = Controller(self.library, server_name=config.name)
        self.controller.load(self.store.load())
        if config.max_downloads is not None:
            self.controller.set_max_downloads(config.max_downloads)
        self.library.scan()
        self.web = WebApp(self.controller, password=config.password)
        # Headsets download on their own TCP port; it only ever serves content.
        content_app = web.Application()
        content_app.router.add_get("/content/{name}", self.web.get_content)
        self.headsets = HeadsetServer(self.controller, config.host, config.tcp_port, config.http_port,
                                      config.public_host, content_app)
        self.beacon = None
        self._runner = None
        self._tasks = []

    @property
    def http_port(self) -> int:
        return self.config.http_port

    @property
    def tcp_port(self) -> int:
        return self.headsets.port

    def _beacon_payload(self) -> dict:
        return {
            "type": "beacon",
            "service": SERVICE_NAME,
            "proto": PROTOCOL_VERSION,
            "version": __version__,
            "server_name": self.config.name,
            "tcp_port": self.headsets.port,
            "http_port": self.config.http_port,
        }

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.analyzer.on_update = lambda: loop.call_soon_threadsafe(self.controller.changed)
        await self.headsets.start()
        self._runner = web.AppRunner(self.web.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.host, self.config.http_port)
        await site.start()
        if self.config.http_port == 0:
            self.config.http_port = self._runner.addresses[0][1]
            self.headsets.http_port = self.config.http_port
        if self.config.discovery:
            addresses = self.config.broadcast or default_broadcast_addresses()
            self.beacon = DiscoveryBeacon(self.config.discovery_port, self._beacon_payload, addresses)
            await self.beacon.start()
            log.info("discovery beacon on UDP %d -> %s", self.config.discovery_port, ", ".join(addresses))
        self.controller.server_info = {
            "http_port": self.config.http_port,
            "tcp_port": self.headsets.port,
            "addresses": local_ipv4_addresses(),
        }
        self._tasks = [asyncio.create_task(self._save_loop()), asyncio.create_task(self._rescan_loop())]
        log.info("headset port TCP %d; dashboard http://%s:%d/", self.headsets.port,
                 (local_ipv4_addresses() or ["localhost"])[0], self.config.http_port)
        log.info("content folder: %s (%d videos)", self.library.root.resolve(), len(self.library.videos))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self.analyzer.on_update = None
        if self.beacon:
            await self.beacon.stop()
        await self.headsets.stop()
        if self._runner:
            await self._runner.cleanup()
        self.analyzer.close()
        self._save()

    def _save(self) -> None:
        self.controller.dirty = False
        try:
            self.store.save(self.controller.dump())
        except OSError as exc:
            log.error("could not save state: %s", exc)

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(SAVE_INTERVAL_S)
            if self.controller.dirty:
                self._save()

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(RESCAN_INTERVAL_S)
            try:
                self.controller.rescan()
            except OSError as exc:
                log.error("library scan failed: %s", exc)


async def serve_forever(config: ServerConfig) -> None:
    server = SyncServer(config)
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
